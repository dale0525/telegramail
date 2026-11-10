"""Small, shared HTML sanitizers for Telegram-rendered content."""

from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup, NavigableString, Tag


_ALLOWED_INLINE_TAGS = {"b", "i", "code", "u", "s", "mark", "tg-spoiler"}
_SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _safe_href(value: object) -> str | None:
    """Return the link target only when Telegram would treat it as a plain link.

    Email content is untrusted, so a scheme Telegram could read as a command
    (``javascript:``, ``tg://``) is dropped and the text is kept instead.
    """

    href = str(value or "").strip()
    return href if href.lower().startswith(_SAFE_LINK_SCHEMES) else None


def sanitize_telegram_limited_html(raw_html: str | None) -> str:
    """Keep only Telegram's safe inline summary tags and strip attributes."""

    if not raw_html:
        return ""
    normalized = str(raw_html).replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    root = BeautifulSoup(f"<div>{normalized}</div>", "html.parser").div

    def render(node: object) -> str:
        if isinstance(node, NavigableString):
            return escape(str(node), quote=False)
        if isinstance(node, Tag):
            name = (node.name or "").lower()
            if name == "br":
                return "\n"
            inner = "".join(render(child) for child in node.contents)
            if name == "a":
                href = _safe_href(node.get("href"))
                return f'<a href="{escape(href, quote=True)}">{inner}</a>' if href else inner
            if name in _ALLOWED_INLINE_TAGS:
                return f"<{name}>{inner}</{name}>"
            return inner
        return ""

    return "".join(render(child) for child in root.contents).strip() if root else ""


def telegram_limited_html_to_text(raw_html: str | None) -> str:
    """Return the visible text of a summary for compact non-HTML previews."""

    sanitized = sanitize_telegram_limited_html(raw_html)
    if not sanitized:
        return ""
    return BeautifulSoup(f"<div>{sanitized}</div>", "html.parser").get_text(" ", strip=True)
