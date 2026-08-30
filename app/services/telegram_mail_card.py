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
