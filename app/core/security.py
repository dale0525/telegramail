"""Telegram initData and signed-session primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    """Raised when a Telegram credential cannot be trusted."""


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    user: dict[str, Any]
    auth_date: int


@dataclass(frozen=True, slots=True)
class Session:
    user_id: int
    csrf_token: str
    expires_at: int


def verify_telegram_init_data(
    init_data: str, *, bot_token: str | None, max_age_seconds: int, now: int | None = None
) -> TelegramIdentity:
    """Validate a Telegram Mini App initData string according to Telegram's HMAC spec."""

    if not bot_token:
        raise AuthenticationError("Telegram authentication is not configured")
    if not init_data:
        raise AuthenticationError("Missing initData")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    supplied_hash: str | None = None
    for key, value in pairs:
        if key == "hash":
            supplied_hash = value
        elif key not in values:
            values[key] = value
    if not supplied_hash:
        raise AuthenticationError("initData hash is missing")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise AuthenticationError("initData hash is invalid")
    try:
        auth_date = int(values["auth_date"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("initData auth_date is invalid") from exc
    timestamp = int(time.time()) if now is None else int(now)
    # A future date is just as untrustworthy as an expired one.  A small skew is
    # allowed for clocks which differ by seconds, not minutes.
    if auth_date > timestamp + 30 or timestamp - auth_date > max_age_seconds:
        raise AuthenticationError("initData has expired")
    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("initData user is invalid") from exc
    return TelegramIdentity(user_id=user_id, user=user, auth_date=auth_date)


def issue_session(*, user_id: int, secret: str, ttl_seconds: int, now: int | None = None) -> tuple[str, Session]:
    issued_at = int(time.time()) if now is None else int(now)
    session = Session(
        user_id=int(user_id), csrf_token=secrets.token_urlsafe(32), expires_at=issued_at + ttl_seconds
    )
    payload = json.dumps(
        {"u": session.user_id, "c": session.csrf_token, "e": session.expires_at},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", session


def read_session(value: str | None, *, secret: str, now: int | None = None) -> Session | None:
    if not value or "." not in value:
        return None
    encoded, supplied_signature = value.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_signature):
        return None
    try:
        decoded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(decoded))
        session = Session(user_id=int(payload["u"]), csrf_token=str(payload["c"]), expires_at=int(payload["e"]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    timestamp = int(time.time()) if now is None else int(now)
    return session if session.expires_at >= timestamp else None
