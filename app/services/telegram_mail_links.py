"""Deterministic, safe action-link handling for Telegram mail cards.

Two sources feed a Telegram keyboard.  The LLM summary supplies structured
``important_links``, and this module derives an unsubscribe link directly from
the message body.  Only the structured sanitizer may consume model output; the
body extractor below is the single place where untrusted provider content is
allowed to become a link, and it matches a fixed vocabulary before applying the
same URL rules as every other link.
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
_UNSUBSCRIBE_CAPTION = "退订"
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
# URL characters that terminate a sentence rather than belong to the link.
_TRAILING_PUNCTUATION = ".,);]>}"


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
        for key in ("important_links", "llm_important_links", "urls", "links", "unsubscribe_links"):
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


def extract_unsubscribe_links(
    html_body: Any = None,
    text_body: Any = None,
    *,
    max_links: int = 1,
) -> list[dict[str, str]]:
    """Return the unsubscribe link a message explicitly offers, if any.

    A sender's unsubscribe affordance is the one link a recipient may always
    need, and models routinely drop it as footer noise.  The HTML anchors are
    consulted first; when they carry no match the plain-text part is scanned for
    a line naming an unsubscribe action, which covers senders that ship a
    text-only rendering.

    Only ``_UNSUBSCRIBE_TERMS`` may select a link, hidden anchors are skipped,
    and the result passes the same HTTP(S)/credential rules as every other
    keyboard link.  An unmatched message yields nothing rather than a guess.
    """

    try:
        limit = min(max(int(max_links), 0), MAX_ACTION_LINKS)
    except (TypeError, ValueError):
        limit = MAX_ACTION_LINKS
    if limit == 0:
        return []

    urls = _unsubscribe_urls_from_html(html_body)
    if not urls:
        urls = _unsubscribe_urls_from_text(text_body)

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        links.append({"caption": _UNSUBSCRIBE_CAPTION, "link": url})
        if len(links) >= limit:
            break
    return links


def merge_mail_action_links(
    unsubscribe: Any,
    llm: Any,
    *,
    max_links: int = MAX_ACTION_LINKS,
) -> list[dict[str, str]]:
    """Combine body-derived and model-derived links into one keyboard.

    The unsubscribe link is placed first so a five-button cap cannot evict it;
    the model's own order is preserved for everything after it.  Both inputs go
    through the structured sanitizer, so a malformed or hostile value is dropped
    instead of reaching Telegram.
    """

    try:
        limit = min(max(int(max_links), 0), MAX_ACTION_LINKS)
    except (TypeError, ValueError):
        limit = MAX_ACTION_LINKS
    if limit == 0:
        return []

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in (unsubscribe, llm):
        for link in sanitize_important_links(source, max_links=limit):
            if link["link"] in seen:
                continue
            seen.add(link["link"])
            merged.append(link)
            if len(merged) >= limit:
                return merged
    return merged


def _unsubscribe_urls_from_html(html_body: Any) -> list[str]:
    if html_body is None or not str(html_body).strip():
        return []
    try:
        soup = BeautifulSoup(str(html_body), "html.parser")
        for tag in soup.find_all(("script", "style", "noscript")):
            tag.decompose()
        urls: list[str] = []
        for anchor in soup.find_all("a", href=True):
            if _is_hidden(anchor):
                continue
            href = str(anchor.get("href") or "").strip()
            label = anchor.get_text(" ", strip=True)
            if not _is_unsubscribe(f"{label.casefold()} {href.casefold()}"):
                continue
            normalized = _safe_url(href)
            if normalized:
                urls.append(normalized)
        return urls
    except Exception:
        # A malformed provider body must never break mail processing.
        return []


def _unsubscribe_urls_from_text(text_body: Any) -> list[str]:
    if text_body is None or not str(text_body).strip():
        return []
    lines = str(text_body).splitlines()
    for index, line in enumerate(lines):
        folded = line.casefold()
        if not _is_unsubscribe(folded):
            continue
        matches = list(_URL_RE.finditer(line))
        if matches:
            for match in _ordered_matches(folded, matches):
                url = _safe_url(_strip_trailing(match.group(0)))
                if url:
                    return [url]
            continue
        # Senders commonly put the URL on the line after the sentence.
        for following in lines[index + 1:]:
            candidate = _strip_url_decoration(following)
            if not candidate:
                break
            if candidate.casefold().startswith("http"):
                match = _URL_RE.match(candidate)
                if match:
                    url = _safe_url(_strip_trailing(match.group(0)))
                    if url:
                        return [url]
            break
    return []


def _ordered_matches(folded: str, matches: list[Any]) -> list[Any]:
    """Order URL matches so the one after the term is tried first."""

    positions = [folded.find(term) for term in _UNSUBSCRIBE_TERMS if term in folded]
    term_index = min(positions) if positions else -1
    after = [match for match in matches if match.start() >= term_index]
    before = [match for match in matches if match.start() < term_index]
    return after + before


def _strip_trailing(url: str) -> str:
    return url.rstrip(_TRAILING_PUNCTUATION)


def _strip_url_decoration(line: str) -> str:
    """Remove plain-text list markers and angle brackets around a URL line.

    A bare URL is only one of the ways senders write one; "- https://..." and
    "<https://...>" are just as common in a text-only rendering.
    """

    value = str(line or "").strip().lstrip("-*").strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


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
    # Telegram rejects a keyboard button whose URL still contains whitespace or
    # control characters, and a rejected keyboard fails the whole delivery.
    # Such a URL is malformed rather than merely unusual, so drop it instead.
    if any(character.isspace() or ord(character) < 0x20 for character in url):
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


def _is_hidden(tag: Tag) -> bool:
    # The element itself counts as well as its ancestors: a link the reader
    # cannot see must never become a visible button.
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag):
            continue
        if node.get("hidden") is not None or str(node.get("aria-hidden", "")).casefold() == "true":
            return True
        style = str(node.get("style", "")).replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
    return False


__all__ = [
    "MAX_ACTION_LINKS",
    "extract_unsubscribe_links",
    "merge_mail_action_links",
    "sanitize_important_links",
]
