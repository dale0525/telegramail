import os
from typing import Mapping, Optional
from urllib.parse import parse_qs, unquote, urlparse

from aiotdlib import ClientProxySettings, ClientProxyType

from app.utils.logger import Logger

logger = Logger().get_logger(__name__)

APP_PROXY_ENV = "TELEGRAMAIL_PROXY"
PROXY_ENV_KEYS = (
    APP_PROXY_ENV,
    "all_proxy",
    "ALL_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
)
NO_PROXY_ENV_KEYS = ("no_proxy", "NO_PROXY")


def _get_env(env: Mapping[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return None

    value = value.strip()
    return value or None


def _no_proxy_disables_all(env: Mapping[str, str]) -> bool:
    for key in NO_PROXY_ENV_KEYS:
        raw = _get_env(env, key)
        if not raw:
            continue

        if any(part.strip() == "*" for part in raw.split(",")):
            return True

    return False


def _select_proxy_url(env: Mapping[str, str]) -> Optional[str]:
    if _no_proxy_disables_all(env):
        return None

    for key in PROXY_ENV_KEYS:
        value = _get_env(env, key)
        if value:
            return value

    return None


def _default_port_for_scheme(scheme: str) -> Optional[int]:
    return {
        "http": 80,
        "https": 443,
        "socks5": 1080,
        "socks5h": 1080,
        "mtproto": 443,
    }.get(scheme)


def build_tdlib_proxy_settings(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[ClientProxySettings]:
    """
    Build TDLib proxy settings from environment variables.

    Supported proxy URL schemes:
    - http://host:port
    - https://host:port (configured as TDLib HTTP proxy)
    - socks5://host:port
    - socks5h://host:port
    - mtproto://host:port?secret=...
    """
    if env is None:
        env = os.environ
    proxy_url = _select_proxy_url(env)
    if not proxy_url:
        return None

    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"

    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    if not host:
        logger.warning("Ignoring proxy URL without host")
        return None

    try:
        port = parsed.port
    except ValueError as e:
        logger.warning(f"Ignoring proxy URL with invalid port: {e}")
        return None

    port = port or _default_port_for_scheme(scheme)
    if port is None:
        logger.warning(f"Ignoring unsupported proxy scheme: {scheme}")
        return None

    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    if scheme in {"http", "https"}:
        proxy_type = ClientProxyType.HTTP
        secret = None
    elif scheme in {"socks5", "socks5h"}:
        proxy_type = ClientProxyType.SOCKS5
        secret = None
    elif scheme == "mtproto":
        proxy_type = ClientProxyType.MTPROTO
        secret = parse_qs(parsed.query).get("secret", [None])[0]
    else:
        logger.warning(f"Ignoring unsupported proxy scheme: {scheme}")
        return None

    if proxy_type == ClientProxyType.MTPROTO and not secret:
        logger.warning("Ignoring MTProto proxy URL without secret")
        return None

    return ClientProxySettings(
        host=host,
        port=port,
        type=proxy_type,
        username=username,
        password=password,
        http_only=False,
        secret=secret,
    )
