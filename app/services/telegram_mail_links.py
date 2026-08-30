"""Deterministic, safe action-link handling for Telegram mail cards.

Email HTML is untrusted input.  Production Telegram paths consume only the
structured LLM link sanitizer below; the older body extractor remains solely
for compatibility with offline callers and must not be used for projections.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag


MAX_ACTION_LINKS = 5
MAX_CAPTION_LENGTH = 42
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_UNSUBSCRIBE_TERMS = (
    "unsubscribe",
    "optout",
    "opt-out",
    "manage preferences",
    "email preferences",
    "subscription preferences",
    "退订",
    "取消订阅",
    "退訂",
    "取消訂閱",
    "解除订阅",
    "解除訂閱",
)
_BROWSER_TERMS = (
    "view in browser",
    "view this email",
    "read online",
    "open in browser",
    "在浏览器中查看",
    "在瀏覽器中查看",
    "网页版本",
    "網頁版本",
)
_PREFERENCE_TERMS = (
    "manage preferences",
    "email preferences",
    "subscription preferences",
    "管理订阅",
    "管理訂閱",
)


def sanitize_important_links(
    links: Any,
    *,
    max_links: int = MAX_ACTION_LINKS,
) -> list[dict[str, str]]:
    """Validate links that already came from the structured LLM result.

    This helper deliberately accepts structured link data (or its persisted
    JSON representation) only.  It never examines an email body, HTML source,
    or a plain-text URL list.  Callers must therefore pass ``mail.important_links``
    or the value read from the corresponding durable JSON column.

    Captions are treated as button text, not Telegram markup: tags and control
    characters are removed, whitespace is collapsed, and the result is bounded
    before it reaches a Bot API keyboard.  URLs are restricted to HTTP(S), have
    no embedded credentials, and are deduplicated while preserving LLM order.
    """

    try:
        limit = min(max(int(max_links), 0), MAX_ACTION_LINKS)
    except (TypeError, ValueError):
        limit = MAX_ACTION_LINKS
    if limit == 0:
        return []

    value = links
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, UnicodeDecodeError):
            # A raw body/URL string is intentionally not a supported input.
            return []

    # The durable column stores a JSON list.  Accept a small amount of
    # compatibility wrapping for adapters that return the decoded row/object.
    if isinstance(value, Mapping):
        wrapped = None
        for key in ("important_links", "llm_important_links", "urls", "links"):
            if key in value:
                wrapped = value.get(key)
                break
        value = wrapped if wrapped is not None else [value]
    if not isinstance(value, (list, tuple)):
        return []

    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        normalized = _safe_url(item.get("link") or item.get("url"))
        if not normalized or normalized in seen:
            continue
        caption = _clean_caption(item.get("caption"))
        if not caption:
            caption = "打开链接"
        cleaned.append({"caption": _truncate(caption), "link": normalized})
        seen.add(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


# Naming aliases keep integrations that call the value an LLM link list
# source-compatible while all aliases retain the same body-free contract.
sanitize_llm_links = sanitize_important_links
sanitize_telegram_links = sanitize_important_links


def extract_email_action_links(
    html_body: str | None = None,
    text_body: str | None = None,
    *,
    max_links: int = MAX_ACTION_LINKS,
) -> list[dict[str, str]]:
    """Return high-value links for a Telegram inline keyboard.

    Unsubscribe/manage-preference links are promoted first, followed by
    browser-view links and other visible call-to-action anchors.  Empty/image
    tracking anchors are ignored.  Plain-text URLs are used only as a fallback
    for messages whose HTML does not expose anchor text.
    """

    limit = min(max(int(max_links), 0), MAX_ACTION_LINKS)
    if limit == 0:
        return []

    candidates: list[tuple[int, int, str, str]] = []
    seen: set[str] = set()
    order = 0

    def add(url: Any, label: Any, *, priority: int, fallback: str = "打开链接") -> None:
        nonlocal order
        normalized = _safe_url(url)
        if not normalized or normalized in seen:
            return
        text = _collapse(label)
        combined = f"{text} {normalized}".casefold()
        if _is_unsubscribe(combined):
            caption = "退订"
            priority = min(priority, 0)
        elif _is_browser_link(combined):
            caption = "在浏览器中查看"
            priority = min(priority, 1)
        elif _is_preference_link(combined):
            caption = "管理订阅"
            priority = min(priority, 1)
        else:
            caption = _truncate(text or fallback)
        seen.add(normalized)
        candidates.append((priority, order, caption, normalized))
        order += 1

    if html_body and str(html_body).strip():
        try:
            soup = BeautifulSoup(str(html_body), "html.parser")
            for link in soup.find_all("a", href=True):
                if _is_hidden(link):
                    continue
                label = link.get_text(" ", strip=True)
                if not label:
                    label = str(link.get("aria-label") or link.get("title") or "").strip()
                if not label:
                    image = link.find("img")
                    label = image.get("alt", "") if isinstance(image, Tag) else ""
                href = str(link.get("href") or "").strip()
                # Anchors with no visible label are commonly tracking pixels.
                # Keep them only when the URL itself clearly carries an action.
                if not label and not _is_action_url(href):
                    continue
                add(href, label, priority=2)
        except Exception:
            # A malformed provider body must not prevent mail delivery.
            pass

    # If HTML had no useful links, recover explicit URLs from the parsed text.
    # This also catches html2text output from HTML-only messages.
    if len(candidates) < limit and text_body:
        for raw in _URL_RE.findall(str(text_body)):
            add(raw.rstrip(".,);]}>"), "", priority=3)
            if len(candidates) >= limit:
                break

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [
        {"caption": caption, "link": url}
        for _, _, caption, url in candidates[:limit]
    ]


def html_to_plain_text(html_body: str | None) -> str:
    """Best-effort visible text for HTML-only messages and card fallbacks."""

    if not html_body or not str(html_body).strip():
        return ""
    try:
        soup = BeautifulSoup(str(html_body), "html.parser")
        for tag in soup.find_all(("script", "style", "noscript")):
            tag.decompose()
        return "\n".join(
            line.strip()
            for line in soup.get_text("\n", strip=True).splitlines()
            if line.strip()
        )
    except Exception:
        return ""


def _clean_caption(value: Any) -> str:
    """Return inert, compact button text from an LLM caption."""

    # LLM output is displayed as Telegram button text (not HTML), but stripping
    # tags here prevents a malformed caption from being mistaken for markup by
    # clients that render button labels richly.
    return _collapse(_HTML_TAG_RE.sub("", str(value or "")))


def _safe_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if len(url) > 4096:
        return None
    if any(ord(character) < 0x20 for character in url):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    # Telegram opens the URL on behalf of the user; do not expose credentials
    # embedded in a provider URL.
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


def _collapse(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())


def _truncate(value: str) -> str:
    if len(value) <= MAX_CAPTION_LENGTH:
        return value or "打开链接"
    return value[: MAX_CAPTION_LENGTH - 1].rstrip() + "…"


def _is_unsubscribe(value: str) -> bool:
    return any(term in value for term in _UNSUBSCRIBE_TERMS)


def _is_browser_link(value: str) -> bool:
    return any(term in value for term in _BROWSER_TERMS)


def _is_preference_link(value: str) -> bool:
    return any(term in value for term in _PREFERENCE_TERMS)


def _is_action_url(value: str) -> bool:
    combined = str(value or "").casefold()
    return _is_unsubscribe(combined) or _is_browser_link(combined) or _is_preference_link(combined)


def _is_hidden(tag: Tag) -> bool:
    if tag.get("hidden") is not None or str(tag.get("aria-hidden", "")).casefold() == "true":
        return True
    for ancestor in tag.parents:
        if not isinstance(ancestor, Tag):
            continue
        style = str(ancestor.get("style", "")).replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
    return False


__all__ = [
    "MAX_ACTION_LINKS",
    "extract_email_action_links",
    "html_to_plain_text",
    "sanitize_important_links",
    "sanitize_llm_links",
    "sanitize_telegram_links",
]
