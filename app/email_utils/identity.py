import email.utils
from email.message import Message
from typing import Iterable, Optional


DELIVERED_TO_HEADERS: list[str] = [
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "X-Envelope-To",
    "X-Google-Original-To",
]


def _normalize_email_address(addr: str) -> str:
    return (addr or "").strip().lower()


def normalize_plus_address(addr: str) -> tuple[str, str]:
    """
    Return (raw, base) where base strips "+tag" from the local-part.

    Example:
      "b+tag@example.com" -> ("b+tag@example.com", "b@example.com")
    """
    raw = _normalize_email_address(addr)
    local, at, domain = raw.partition("@")
    if not at:
        return raw, raw
    if "+" not in local:
        return raw, raw
    base_local = local.split("+", 1)[0]
    base = f"{base_local}@{domain}"
    return raw, base


def extract_delivered_to_candidates(msg: Message) -> list[str]:
    """
    Extract "actual delivered-to" candidates from provider-specific headers.

    Returns lower-cased, de-duplicated addresses in header priority order.
    """
    if msg is None:
        return []

    results: list[str] = []
    seen: set[str] = set()

    for header in DELIVERED_TO_HEADERS:
        for raw_value in msg.get_all(header, []) or []:
            for _name, addr in email.utils.getaddresses([raw_value]):
                normalized = _normalize_email_address(addr)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                results.append(normalized)

    return results


def choose_recommended_from(
    *,
    candidates: Iterable[str],
    identity_emails: set[str],
    default_email: str,
) -> str:
    """
    Choose a From identity email.

    Rule:
      - If delivered-to raw matches identity, use it.
      - Else, if it's a plus address and base matches identity, use base.
      - Else, fall back to default_email.
    """
    identity_set = {_normalize_email_address(e) for e in (identity_emails or set())}
    default_norm = _normalize_email_address(default_email)

    for candidate in candidates or []:
        raw, base = normalize_plus_address(candidate)
        if raw in identity_set:
            return raw
        if base in identity_set:
            return base

    return default_norm


def suggest_identity(*, candidates: Iterable[str], identity_emails: set[str]) -> Optional[str]:
    """
    Suggest a new From identity to add based on Delivered-To candidates.

    - If candidate is plus-addressed and base is missing, suggest base.
    - Else if candidate is missing, suggest candidate.
    """
    identity_set = {_normalize_email_address(e) for e in (identity_emails or set())}

    for candidate in candidates or []:
        raw, base = normalize_plus_address(candidate)
        if raw != base:
            if base not in identity_set:
                return base
            # Base already exists; do not suggest the raw +tag address.
            continue
        if raw and raw not in identity_set:
            return raw

    return None
