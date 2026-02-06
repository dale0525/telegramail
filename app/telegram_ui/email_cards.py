from __future__ import annotations

import html
from typing import Optional

from app.i18n import _

_DIVIDER = "────────────"


def _escape(text: object) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _normalize_single_line(text: str) -> str:
    return " ".join((text or "").strip().split())


def _ellipsize(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if max_len <= 0:
        return ""
    if len(t) <= max_len:
        return t
    if max_len == 1:
        return "…"
    return t[: max_len - 1].rstrip() + "…"


def _escape_and_ellipsize(text: object, max_len: int) -> str:
    return _escape(_ellipsize(str(text or ""), max_len))


def _escape_and_truncate_to_fit(text: str, max_len: int) -> str:
    """
    Escape plain text for Telegram HTML parse mode and truncate so the escaped
    string length does not exceed max_len.
    """
    if max_len <= 0:
        return ""
    raw = (text or "").strip()
    escaped_full = _escape(raw)
    if len(escaped_full) <= max_len:
        return escaped_full

    suffix = _escape(f"...\n\n{_('content_truncated')}")
    max_content_len = max(0, max_len - len(suffix))
    if max_content_len <= 0:
        return suffix[:max_len]

    # Binary search the longest prefix whose escaped length fits.
    lo, hi = 0, len(raw)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_escape(raw[:mid])) <= max_content_len:
            lo = mid
        else:
            hi = mid - 1

    trimmed = raw[:lo].rstrip()
    return _escape(trimmed) + suffix


def build_incoming_email_card(
    *,
    subject: str,
    sender: str,
    recipient: str = "",
    cc: str = "",
    bcc: str = "",
    mailbox: str = "",
    email_date: str = "",
    attachments_count: int = 0,
    summary_html: Optional[str] = None,
    body_text: Optional[str] = None,
    max_chars: int = 4000,
) -> str:
    subject_clean = _normalize_single_line(subject) or _("no_subject")
    sender_clean = (sender or "").strip()
    recipient_clean = (recipient or "").strip()
    mailbox_clean = _normalize_single_line(str(mailbox or ""))

    lines: list[str] = [
        f"<code>IN</code> 📥 <b>{_escape_and_ellipsize(subject_clean, 200)}</b>",
        f"✍️ {_escape(_('email_from'))}: {_escape_and_ellipsize(sender_clean, 500)}",
    ]

    if recipient_clean:
        lines.append(
            f"📮 {_escape(_('email_to'))}: {_escape_and_ellipsize(recipient_clean, 700)}"
        )
    if cc and str(cc).strip():
        lines.append(
            f"👥 {_escape(_('email_cc'))}: {_escape_and_ellipsize(str(cc).strip(), 700)}"
        )
    if bcc and str(bcc).strip():
        lines.append(
            f"🔒 {_escape(_('email_bcc'))}: {_escape_and_ellipsize(str(bcc).strip(), 700)}"
        )
    if mailbox_clean:
        lines.append(
            f"📁 {_escape(_('email_mailbox'))}: {_escape_and_ellipsize(mailbox_clean, 200)}"
        )
    if email_date and str(email_date).strip():
        lines.append(f"🕒 {_escape_and_ellipsize(str(email_date).strip(), 200)}")
    if int(attachments_count or 0) > 0:
        lines.append(f"📎 {_escape(_('draft_attachments'))}: {int(attachments_count)}")

    base = "\n".join(lines).strip()
    if not base:
        base = "<code>IN</code>"

    if summary_html and str(summary_html).strip():
        summary = str(summary_html).strip()
        # The summary formatter already sanitizes for Telegram-limited HTML.
        return (
            f"{base}\n{_DIVIDER}\n"
            f"<b>{_escape(_('email_summary'))}:</b>\n"
            f"{summary}"
        )[:max_chars]

    if body_text is None:
        body_text = f"📧 {_('email_content_unavailable')}"

    prefix = f"{base}\n{_DIVIDER}\n"
    available = max(0, max_chars - len(prefix))
    body = _escape_and_truncate_to_fit(str(body_text), available)
    return prefix + body


def build_outgoing_email_card(
    *,
    subject: str,
    from_display: str,
    to_addrs: str = "",
    cc_addrs: str = "",
    bcc_addrs: str = "",
    body_text: str = "",
    max_chars: int = 4000,
) -> str:
    subject_clean = _normalize_single_line(subject) or _("no_subject")
    from_clean = (from_display or "").strip()

    lines: list[str] = [
        f"<code>OUT</code> 📤 <b>{_escape_and_ellipsize(subject_clean, 200)}</b>",
        f"✍️ {_escape(_('email_from'))}: {_escape_and_ellipsize(from_clean, 500)}",
    ]

    if to_addrs and str(to_addrs).strip():
        lines.append(
            f"📮 {_escape(_('email_to'))}: {_escape_and_ellipsize(str(to_addrs).strip(), 700)}"
        )
    if cc_addrs and str(cc_addrs).strip():
        lines.append(
            f"👥 {_escape(_('email_cc'))}: {_escape_and_ellipsize(str(cc_addrs).strip(), 700)}"
        )
    if bcc_addrs and str(bcc_addrs).strip():
        lines.append(
            f"🔒 {_escape(_('email_bcc'))}: {_escape_and_ellipsize(str(bcc_addrs).strip(), 700)}"
        )

    base = "\n".join(lines).strip()
    if not base:
        base = "<code>OUT</code>"

    body_clean = (body_text or "").strip()
    if not body_clean:
        return base[:max_chars]

    prefix = f"{base}\n{_DIVIDER}\n"
    available = max(0, max_chars - len(prefix))
    body = _escape_and_truncate_to_fit(body_clean, available)
    return prefix + body
