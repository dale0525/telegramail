"""RFC-aware message composition with deterministic operation Message-IDs."""
from __future__ import annotations

import hashlib
import re
from email.utils import getaddresses, make_msgid
from typing import Iterable

from app.email_utils.markdown_render import render_markdown_to_html
from app.email_utils.smtp_client import build_email_message

from .types import MailDraft


class RecipientRequired(ValueError):
    """Raised before enqueueing a draft with no envelope recipient."""


def normalize_addresses(values: Iterable[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    raw = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    seen: set[str] = set()
    for _, addr in getaddresses(raw):
        addr = addr.strip()
        if not addr:
            continue
        key = addr.casefold()
        if key not in seen:
            seen.add(key)
            out.append(addr)
    return tuple(out)


def stable_message_id(operation_id: str, from_email: str) -> str:
    """Create the same RFC Message-ID for every retry of one operation."""
    domain = (from_email.rsplit("@", 1)[-1].strip().lower() if "@" in from_email else "telegramail.local")
    safe_domain = re.sub(r"[^a-z0-9.-]", "-", domain) or "telegramail.local"
    digest = hashlib.sha256(f"{operation_id}|{from_email.casefold()}".encode()).hexdigest()[:32]
    return f"<{digest}@{safe_domain}>"


def _references(parent_message_id: str | None, parent_references: Iterable[str] | str | None) -> tuple[str, ...]:
    source = [parent_references] if isinstance(parent_references, str) else (parent_references or ())
    refs: list[str] = []
    for item in source:
        refs.extend(re.findall(r"<[^>]+>", item or ""))
    if parent_message_id and parent_message_id not in refs:
        refs.append(parent_message_id)
    return tuple(refs)


class MailComposer:
    @staticmethod
    def compose(draft: MailDraft):
        to = normalize_addresses(draft.to)
        cc = normalize_addresses(draft.cc)
        bcc = normalize_addresses(draft.bcc)
        if not (to or cc or bcc):
            raise RecipientRequired("at least one To, Cc, or Bcc recipient is required")
        html = draft.html_body
        if html is None and draft.markdown_body is not None:
            html = render_markdown_to_html(draft.markdown_body)
        message_id = draft.message_id or stable_message_id(draft.operation_id, draft.from_email)
        msg = build_email_message(
            from_email=draft.from_email,
            from_name=draft.from_name,
            to_addrs=list(to),
            cc_addrs=list(cc),
            subject=draft.subject,
            text_body=draft.text_body or draft.markdown_body or "",
            html_body=html,
            reply_to=draft.reply_to,
            in_reply_to=draft.in_reply_to,
            references=list(draft.references),
            message_id=message_id,
            attachments=[{"filename": a.filename, "data": a.data, "mime_type": a.mime_type} for a in draft.attachments],
        )
        return msg, message_id, to + cc + bcc

    @classmethod
    def reply(cls, draft: MailDraft, parent_message_id: str, parent_references: Iterable[str] | str | None = None) -> MailDraft:
        return MailDraft(**{**draft.__dict__, "in_reply_to": parent_message_id, "references": _references(parent_message_id, parent_references)})

    @classmethod
    def reply_all(
        cls,
        draft: MailDraft,
        *,
        parent_from: Iterable[str] | str | None,
        parent_to: Iterable[str] | str | None = None,
        parent_cc: Iterable[str] | str | None = None,
        parent_message_id: str,
        parent_references: Iterable[str] | str | None = None,
    ) -> MailDraft:
        own = {a.casefold() for a in normalize_addresses([draft.from_email, draft.reply_to or ""])}
        to = normalize_addresses(parent_from)
        cc = normalize_addresses(list(normalize_addresses(parent_to)) + list(normalize_addresses(parent_cc)))
        to = tuple(a for a in to if a.casefold() not in own)
        to_keys = {a.casefold() for a in to}
        cc = tuple(a for a in cc if a.casefold() not in own and a.casefold() not in to_keys)
        return MailDraft(**{**draft.__dict__, "to": to, "cc": cc, "in_reply_to": parent_message_id, "references": _references(parent_message_id, parent_references)})

    @classmethod
    def forward(cls, draft: MailDraft) -> MailDraft:
        # A forward starts a fresh thread; callers may include quoted content in body.
        return MailDraft(**{**draft.__dict__, "in_reply_to": None, "references": ()})


compose = MailComposer.compose
reply = MailComposer.reply
reply_all = MailComposer.reply_all
forward = MailComposer.forward
