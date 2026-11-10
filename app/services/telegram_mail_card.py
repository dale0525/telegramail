"""Safe, deliberately small email cards for Telegram projections.

Telegram is the quick-reading surface. The card contains only subject, sender
and summary; links/actions live in the reply keyboard and raw provider HTML is
uploaded separately by the projection adapter.
"""

from __future__ import annotations

import html
from typing import Any

from app.integrations.telegram_http import MAX_MESSAGE_LENGTH, split_html
from app.services.telegram_html import sanitize_telegram_limited_html


def _plain_text(value: Any, *, fallback: str = "—", single_line: bool = False) -> str:
    """Coerce a parsed field to text without interpreting it as markup."""

    text = str(value or "").replace("\x00", "").strip()
    if single_line:
        text = " ".join(text.split())
    return text or fallback


def _escaped(value: Any, *, fallback: str = "—", single_line: bool = False) -> str:
    return html.escape(_plain_text(value, fallback=fallback, single_line=single_line), quote=True)


def build_rich_mail_card(
    *,
    subject: Any,
    sender: Any,
    received_at: Any = None,
    body_text: Any = "",
    summary: Any = None,
    category: Any = None,
    priority: Any = None,
) -> str:
    """Return one structured rich-message card for a parsed, untrusted email.

    The structure (heading, sender line, divider, summary) is fixed chrome; every
    dynamic value is escaped first and the LLM summary passes the same restricted
    sanitizer as the plain card, so no provider HTML reaches Telegram's parser.
    """

    summary_text = _plain_text(summary, fallback="")
    rendered_content = (
        sanitize_telegram_limited_html(summary_text)
        if summary_text
        else _escaped(body_text, fallback="(无可用内容)")
    )
    # The rich card is one message, so an over-long summary is cut at the
    # rendered-text boundary instead of spawning further messages. split_html
    # keeps tags balanced, which is what rich parsing requires, and the fixed
    # chrome is subtracted first so the whole card stays inside the limit.
    chrome = (
        f"📧 {_plain_text(subject, fallback='(无主题)', single_line=True)}",
        _plain_text(sender, single_line=True),
        "摘要",
    )
    summary_budget = MAX_MESSAGE_LENGTH - sum(len(line) for line in chrome) - 8
    rendered_content = split_html(rendered_content, limit=max(1, summary_budget))[0]
    lines = (
        f"<h3>📧 {_escaped(subject, fallback='(无主题)', single_line=True)}</h3>",
        f"<p>{_escaped(sender, single_line=True)}</p>",
        "<hr/>",
        "<p><b>摘要</b></p>",
        f"<p>{rendered_content}</p>",
    )
    return "\n".join(lines)


def has_projectable_mail_content(
    *,
    body_text: Any,
    summary: Any = None,
    html_body: Any = None,
    attachments: Any = None,
) -> bool:
    """Whether a mail has text that warrants creating a new Telegram topic.

    Headers alone are not projected into a fresh topic: that would create a
    misleading empty conversation when an IMAP message has no readable body.
    """

    return bool(
        _plain_text(summary, fallback="")
        or _plain_text(body_text, fallback="")
        or _plain_text(html_body, fallback="")
        or any(not bool(getattr(item, "is_inline", False)) for item in tuple(attachments or ()))
    )


def build_safe_mail_card(
    *,
    subject: Any,
    sender: Any,
    received_at: Any = None,
    body_text: Any = "",
    summary: Any = None,
    category: Any = None,
    priority: Any = None,
    max_message_length: int = MAX_MESSAGE_LENGTH,
) -> list[str]:
    """Return one bounded Telegram-HTML card for a parsed, untrusted email.

    Parser-produced body text is escaped. LLM summaries are passed through a
    restricted sanitizer so intentional ``<b>``, ``<i>`` and ``<code>`` markup
    renders while arbitrary email/provider HTML stays inert. Historical metadata
    arguments stay accepted for source compatibility but are not rendered.
    """

    if max_message_length < 1:
        raise ValueError("max_message_length must be positive")
    limit = min(max_message_length, MAX_MESSAGE_LENGTH)
    summary_text = _plain_text(summary, fallback="")
    content = summary_text if summary_text else body_text
    rendered_content = (
        sanitize_telegram_limited_html(summary_text)
        if summary_text
        else _escaped(content, fallback="(无可用内容)")
    )
    lines = (
        "<b>📧 邮件</b>",
        f"<b>主题：</b> {_escaped(subject, fallback='(无主题)', single_line=True)}",
        f"<b>发件人：</b> {_escaped(sender, single_line=True)}",
        "<b>摘要：</b>",
        rendered_content,
    )
    fragments = split_html("\n".join(lines), limit=limit)
    # Keep one user-visible card even for malformed/very long summaries. The
    # first fragment is balanced by split_html and is enough for a preview;
    # another message would create the unread noise this format avoids.
    return fragments[:1]
