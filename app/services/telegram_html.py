"""Small, shared HTML sanitizers for Telegram-rendered content."""

from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup, NavigableString, Tag


_ALLOWED_INLINE_TAGS = {"b", "i", "code"}


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
