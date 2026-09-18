"""Select the body text used for summaries, cards, and the Mini App.

Provider mail is untrusted input with two representations: the text/plain part
and the text/html part.  Some senders ship a stub plain-text part ("your email
client might not support HTML") while the HTML part carries the real content, so
preferring the plain-text part unconditionally produces a useless summary and an
empty-looking Telegram card.

This module owns that decision.  It is presentation-only: the caller decides
what a selected body is allowed to become, and nothing here promotes body
content into Telegram markup.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

# Markers that identify a stub plain-text part shipped by senders whose real
# content lives in the HTML part.  Matched as casefolded substrings.
_PLACEHOLDER_MARKERS = (
    "not support html",
    "support html formatted",
    "trouble viewing this email",
    "view this email in your browser",
    "view this email in a browser",
    "在浏览器中查看此邮件",
    "如果邮件无法正常显示",
    "无法显示此邮件",
)
# A genuine newsletter can also mention a browser link in its footer.  Only a
# short body can be a stub, so the marker alone is not sufficient.
_PLACEHOLDER_MAX_LENGTH = 1500


def is_placeholder_text_body(text: Any) -> bool:
    """Whether a plain-text part is a stub for HTML-only content.

    A short body that names an HTML rendering problem is a placeholder.  The
    length bound keeps a real newsletter with a "view in browser" footer from
    being mistaken for one.
    """

    value = str(text or "").strip()
    if not value or len(value) > _PLACEHOLDER_MAX_LENGTH:
        return False
    folded = value.casefold()
    return any(marker in folded for marker in _PLACEHOLDER_MARKERS)


def prepare_email_body(html_body: Any, text_body: Any) -> str:
    """Return the body text a reader should see.

    The plain-text part wins whenever it is real content.  It is discarded only
    when it is empty or a placeholder, in which case the rendered HTML part is
    used instead.
    """

    text = str(text_body or "").strip()
    if text and not is_placeholder_text_body(text):
        return str(text_body or "")
    return html_to_plain_text(html_body)


def html_to_plain_text(html_body: Any) -> str:
    """Best-effort visible text for HTML-only messages and card fallbacks.

    Link targets are appended after their label because a rendered body is also
    read by the summarizer, which needs to see the destinations that a text-only
    rendering would otherwise drop.
    """

    if html_body is None or not str(html_body).strip():
        return ""
    try:
        soup = BeautifulSoup(str(html_body), "html.parser")
        for tag in soup.find_all(("script", "style", "noscript")):
            tag.decompose()
        for link in soup.find_all("a", href=True):
            href = str(link.get("href") or "").strip()
            label = link.get_text(" ", strip=True)
            if href.startswith(("http://", "https://")):
                link.replace_with(f"{label} ({href})" if label else href)
            else:
                link.replace_with(label)
        return "\n".join(
            line.strip()
            for line in soup.get_text("\n", strip=True).splitlines()
            if line.strip()
        )
    except Exception:
        return ""


__all__ = [
    "html_to_plain_text",
    "is_placeholder_text_body",
    "prepare_email_body",
]
