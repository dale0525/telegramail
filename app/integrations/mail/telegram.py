"""Telegram Bot API projection adapter for the transport-neutral mail workers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Callable

from app.services.telegram_mail_card import build_safe_mail_card, has_projectable_mail_content
from app.services.telegram_mail_links import html_to_plain_text, sanitize_important_links
from app.services.telegram_mail_ui import topic_action_keyboard, topic_delete_keyboard
from app.integrations.telegram_http import MiniAppOnlyProjection, is_missing_forum_topic_error

from .types import IncomingMail


@dataclass(frozen=True)
class TelegramTopic:
    chat_id: int | str
    message_thread_id: int
    db_thread_id: int | None = None


class ProjectionWaitingBinding(RuntimeError):
    """No administrator private chat has been bound yet; retry projection later."""


class ProjectionDeliveryError(RuntimeError):
    """A projection failed after zero or more Telegram messages were accepted."""

    def __init__(self, *, sent_any: bool, ambiguous: bool = False,
                 topic_missing: bool = False) -> None:
        self.sent_any = sent_any
        self.ambiguous = ambiguous
        self.topic_missing = topic_missing
        super().__init__(type(self).__name__)


def format_topic_name(sender: Any, subject: Any, *, limit: int = 128) -> str:
    """Return the stable human-readable Topic title used for new threads.

    Telegram accepts at most 128 Unicode characters for a forum-topic name.
    Remove control characters and collapse whitespace before truncating so a
    malformed provider header cannot inject line breaks or produce an invalid
    title.  Existing Topic mappings are reused by the repository and therefore
    do not call this function again.
    """

    if limit < 1:
        raise ValueError("limit must be positive")

    def clean(value: Any, fallback: str) -> str:
        text = str(value or "").replace("\x00", "")
        text = " ".join(text.split())
        return text or fallback

    name = f"{clean(sender, '未知发件人')} · {clean(subject, '(无主题)')}"
    return truncate_topic_name(name, limit=limit)


def truncate_topic_name(value: Any, *, limit: int = 128) -> str:
    """Sanitize/truncate an already-composed Topic name without adding text."""

    if limit < 1:
        raise ValueError("limit must be positive")
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[: int(limit)].rstrip() or "邮件"


class MailTelegramProjection:
    def __init__(
        self,
        client: Any,
        forum_chat_id: int | str | Callable[[Any], int | str | None],
        *,
        repository: Any = None,
        after_delivery: Callable[[], Any] | None = None,
    ) -> None:
        self.client, self.forum_chat_id = client, forum_chat_id
        self.repository = repository
        self.after_delivery = after_delivery

    def _chat_id(self, account: Any) -> int | str:
        value = self.forum_chat_id(account) if callable(self.forum_chat_id) else self.forum_chat_id
        if value is None:
            raise ProjectionWaitingBinding("administrator private chat is not bound")
        return value

    async def ensure_private_topic(self, account: Any, subject: str) -> TelegramTopic:
        chat_id = self._chat_id(account)
        result = await self.client.create_forum_topic(chat_id, truncate_topic_name(subject))
        return TelegramTopic(chat_id, int(result["message_thread_id"]))

    @staticmethod
    def has_projectable_content(mail: IncomingMail) -> bool:
        return has_projectable_mail_content(
            body_text=mail.text_body,
            summary=mail.summary,
            html_body=mail.html_body,
            attachments=mail.attachments,
        )

    async def project_mail(self, mail: IncomingMail, topic: Any) -> Any:
        if not isinstance(topic, TelegramTopic):
            raise ValueError("mail projection requires a persisted Telegram topic")
        if not self.has_projectable_content(mail):
            return []
        # Never pass untrusted mail HTML to Telegram's parser. The card service
        # accepts parsed plain text only and escapes every dynamic value before
        # wrapping the fixed presentation chrome in Telegram-supported HTML.
        card_body = mail.text_body or html_to_plain_text(mail.html_body)
        text_fragments = build_safe_mail_card(subject=mail.subject, sender=mail.sender,
                                              received_at=mail.received_at, body_text=card_body,
                                              summary=mail.summary, category=mail.category,
                                              priority=mail.priority)
        # Captions have a separate, smaller limit than normal messages. The
        # card builder returns a balanced first fragment, so this is always a
        # single Telegram message/caption.
        caption_fragments = build_safe_mail_card(
            subject=mail.subject, sender=mail.sender, received_at=mail.received_at,
            body_text=card_body, summary=mail.summary, category=mail.category,
            priority=mail.priority, max_message_length=1024,
        )
        text_card = text_fragments[0] if text_fragments else ""
        caption = caption_fragments[0] if caption_fragments else text_card[:1024]
        # Action buttons may only come from the structured LLM result carried by
        # ``IncomingMail.important_links``.  Never recover URLs from the mail
        # body here: that path turns arbitrary provider content into Telegram
        # actions and is intentionally retired for production projections.
        action_links = _mail_important_links(mail)
        results: list[Any] = []
        primary_message_kind = "text"

        def keyboard() -> dict[str, Any] | None:
            if topic.db_thread_id is not None:
                return topic_delete_keyboard(topic.db_thread_id, action_links=action_links)
            if action_links:
                return topic_action_keyboard(action_links)
            return None

        def append_visible(result: Any) -> None:
            values = result if isinstance(result, list) else [result]
            visible = [item for item in values if isinstance(item, Mapping) and item.get("message_id") is not None]
            if not visible:
                raise RuntimeError("Telegram projection returned no visible message")
            results.extend(values)

        try:
            send_document = getattr(self.client, "send_document", None)
            html_value = str(mail.html_body or "").strip()
            if html_value and callable(send_document):
                document_kwargs: dict[str, Any] = {
                    "filename": _html_filename(mail.subject),
                    "content_type": "text/html",
                    "message_thread_id": topic.message_thread_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "disable_notification": False,
                }
                if keyboard() is not None:
                    document_kwargs["reply_markup"] = keyboard()
                document_result = await send_document(
                    topic.chat_id,
                    html_value.encode("utf-8", errors="replace"),
                    **document_kwargs,
                )
                if not isinstance(document_result, MiniAppOnlyProjection) and not getattr(document_result, "mini_app_only", False):
                    primary_message_kind = "html"
                    append_visible(document_result)
                else:
                    # Bot API's document-size limit is a normal fallback, not
                    # a successful delivery without a user-visible card.
                    fallback_kwargs: dict[str, Any] = {
                        "message_thread_id": topic.message_thread_id,
                        "parse_mode": "HTML",
                        "disable_notification": False,
                    }
                    if keyboard() is not None:
                        fallback_kwargs["reply_markup"] = keyboard()
                    result = await self.client.send_message(topic.chat_id, text_card, **fallback_kwargs)
                    primary_message_kind = "text"
                    append_visible(result)
            else:
                text_kwargs: dict[str, Any] = {
                    "message_thread_id": topic.message_thread_id,
                    "parse_mode": "HTML",
                    "disable_notification": False,
                }
                if keyboard() is not None:
                    text_kwargs["reply_markup"] = keyboard()
                result = await self.client.send_message(topic.chat_id, text_card, **text_kwargs)
                primary_message_kind = "text"
                append_visible(result)

            # Persisted/non-inline MIME parts are sent as quiet follow-up
            # documents. CID images are intentionally excluded: they belong to
            # the offline HTML resource set, not to the Topic conversation.
            for attachment in tuple(getattr(mail, "attachments", ()) or ()):
                if not hasattr(attachment, "data") or bool(getattr(attachment, "is_inline", False)):
                    continue
                if not callable(send_document):
                    continue
                attachment_result = await send_document(
                    topic.chat_id,
                    bytes(getattr(attachment, "data") or b""),
                    filename=str(getattr(attachment, "filename", None) or "attachment"),
                    content_type=str(getattr(attachment, "mime_type", None) or "application/octet-stream"),
                    message_thread_id=topic.message_thread_id,
                    disable_notification=True,
                )
                if isinstance(attachment_result, MiniAppOnlyProjection) or getattr(attachment_result, "mini_app_only", False):
                    continue
                append_visible(attachment_result)
        except Exception as exc:
            # A timeout/transport error may have reached Telegram. Keep that
            # distinction through the projection layer: deleting the just-created
            # topic would then risk erasing a message the user can already see.
            raise ProjectionDeliveryError(
                sent_any=bool(results),
                ambiguous=bool(getattr(exc, "ambiguous", False)),
                topic_missing=is_missing_forum_topic_error(exc),
            ) from exc
        if results and isinstance(results[0], Mapping):
            # Carry the primary Bot API message shape to the lease owner so it
            # can persist whether later summary refreshes must edit a document
            # caption or a text message. This distinguishes oversized-HTML
            # fallbacks from normal HTML deliveries after process restarts.
            results[0] = {**results[0], "message_kind": primary_message_kind}
        if self.repository is not None and getattr(mail, "email_id", None) is not None:
            recorder = getattr(self.repository, "record_telegram_delivery_parts", None)
            if callable(recorder):
                message_ids = [
                    int(item["message_id"])
                    for item in results
                    if isinstance(item, dict) and item.get("message_id") is not None
                ]
                if len(message_ids) > 1:
                    # The lease-owned part 0 is completed by the projection
                    # worker; only persist the additional chunks here.
                    try:
                        recorder(int(mail.email_id), int(topic.chat_id), message_ids[1:], start_index=1)
                    except Exception as exc:
                        # Telegram already accepted the messages.  Preserve
                        # that uncertainty so the worker quarantines the row
                        # instead of compensating by deleting a live Topic.
                        raise ProjectionDeliveryError(sent_any=True, ambiguous=True) from exc
        if self.after_delivery is not None:
            try:
                callback_result = self.after_delivery()
                if hasattr(callback_result, "__await__"):
                    await callback_result
            except Exception:
                # The email has already been delivered.  A convenience Inbox
                # refresh must not turn that successful projection into a retry.
                pass
        return results

    async def update_mail_summary(self, mail: IncomingMail, analysis: dict[str, Any] | None = None) -> bool:
        """Refresh already-delivered Topic card messages in place.

        The summary worker calls this after persisting the analysis.  We only
        edit message ids recorded in ``telegram_delivery_parts``; if a message
        has not reached Telegram yet (or the mail predates the v2 mapping), the
        method is a safe no-op and the normal projection path remains owner of
        delivery.
        """
        repository = self.repository
        email_id = getattr(mail, "email_id", None)
        if repository is None:
            return False
        email_row = None
        if email_id is not None:
            getter = getattr(repository, "get_email", None)
            if callable(getter):
                email_row = getter(int(email_id))
        if email_row is None:
            lookup = getattr(repository, "get_email_by_imap_uid", None)
            if callable(lookup):
                email_row = lookup(
                    int(mail.account_id),
                    mailbox=mail.mailbox,
                    uid=mail.uid,
                    uidvalidity=mail.uidvalidity,
                )
            if email_row:
                email_id = email_row.get("id")
        if email_id is None:
            return False
        getter = getattr(repository, "list_telegram_delivery_parts", None)
        if not callable(getter):
            return False
        parts = getter(int(email_id))
        if not parts:
            return False
        thread_id = email_row.get("thread_id") if email_row else None
        # Prefer the freshly generated links on the mail value, then use the
        # persisted JSON row when a summary job was restored after a restart.
        # Neither source requires parsing the untrusted message body.
        action_links = _mail_important_links(mail, analysis=analysis, row=email_row)
        card_body = mail.text_body or html_to_plain_text(mail.html_body)
        text_fragments = build_safe_mail_card(
            subject=mail.subject,
            sender=mail.sender,
            received_at=mail.received_at,
            body_text=card_body,
            summary=mail.summary,
            category=mail.category,
            priority=mail.priority,
        )
        caption_fragments = build_safe_mail_card(
            subject=mail.subject,
            sender=mail.sender,
            received_at=mail.received_at,
            body_text=card_body,
            summary=mail.summary,
            category=mail.category,
            priority=mail.priority,
            max_message_length=1024,
        )
        edited = False
        for index, part in enumerate(parts):
            # Part 0 is the card/document. Additional parts are immutable
            # attachments and must never be replaced by a summary refresh.
            if index != 0:
                continue
            message_id = part.get("telegram_message_id")
            chat_id = part.get("telegram_chat_id")
            if message_id is None or chat_id is None:
                continue
            reply_markup = None
            if thread_id is not None and (index == 0 or index == len(parts) - 1):
                reply_markup = topic_delete_keyboard(
                    int(thread_id),
                    action_links=action_links if index == 0 else None,
                )
            kind = str(part.get("kind") or part.get("message_kind") or "")
            # Older rows do not carry an explicit kind. In that case body_html
            # is a reliable indication for normal (non-size-fallback) HTML
            # deliveries; explicit rows win when the fallback was recorded.
            is_html = kind == "html" or (not kind and bool(email_row and email_row.get("body_html")))
            if is_html:
                await self.client.edit_message_caption(
                    int(chat_id), int(message_id), caption_fragments[0], parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                await self.client.edit_message_text(
                    int(chat_id), int(message_id), text_fragments[0], parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            edited = True
        return edited

    async def delete_topic(self, topic: Any) -> bool:
        if not isinstance(topic, TelegramTopic):
            raise ValueError("Telegram topic mapping is missing")
        # A missing/invalid thread id is deliberately *not* treated as a
        # successful deletion here.  The Bot API uses the same errors for a
        # stale mapping and for a topic that still exists under a different
        # mapping.  Marking the phase complete in either case makes the local
        # delete saga tombstone the mail while leaving the Telegram Topic in
        # place.  The worker must retain ``provider_deleted`` and retry (with
        # the error recorded) until Telegram explicitly confirms ``True``.
        result = await self.client.delete_forum_topic(topic.chat_id, topic.message_thread_id)
        return result is True


def _mail_important_links(
    mail: Any,
    *,
    analysis: Mapping[str, Any] | None = None,
    row: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Read only structured/persisted LLM links for Telegram keyboards.

    ``IncomingMail`` gained ``important_links`` after the first v2 rollout;
    keeping the row/analysis fallbacks here lets summary refresh work across a
    rolling deployment while the durable column is being backfilled.  Deliberately
    absent are ``text_body``/``html_body`` fallbacks: body URL extraction is no
    longer a production action-link source.
    """

    candidates: list[Any] = []
    value = _field(mail, "important_links")
    if value is not None:
        candidates.append(value)

    # ``urls`` is the pre-v2 structured LLM key.  It remains a compatibility
    # source only when explicitly supplied by the analysis object; it is never
    # inferred from message text.
    if isinstance(analysis, Mapping):
        for key in ("important_links", "urls"):
            if key in analysis and analysis.get(key) is not None:
                candidates.append(analysis.get(key))

    if isinstance(row, Mapping):
        for key in (
            "important_links",
            "llm_important_links_json",
            "important_links_json",
        ):
            if key in row and row.get(key) is not None:
                candidates.append(row.get(key))

    for candidate in candidates:
        cleaned = sanitize_important_links(candidate)
        if cleaned:
            return cleaned
    return []


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _html_filename(subject: str | None) -> str:
    """Create a readable, filesystem-safe name for Telegram's HTML document."""

    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(subject or "")).strip(" ._")
    if not name:
        name = "email"
    return f"{name[:80].rstrip() or 'email'}.html"
