import os
from typing import Mapping, Optional
from urllib.parse import parse_qs, unquote, urlparse

from aiotdlib import ClientProxySettings, ClientProxyType

from app.utils.logger import Logger

logger = Logger().get_logger(__name__)

APP_PROXY_ENV = "TELEGRAMAIL_PROXY"
FALLBACK_PROXY_ENV_KEYS = (
    "all_proxy",
    "ALL_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
)
NO_PROXY_ENV_KEYS = ("no_proxy", "NO_PROXY")
SUPPORTED_PROXY_SCHEMES = {"http", "socks5", "socks5h", "mtproto"}


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


def _default_port_for_scheme(scheme: str) -> Optional[int]:
    return {
        "http": 80,
        "socks5": 1080,
        "socks5h": 1080,
        "mtproto": 443,
    }.get(scheme)


def _parse_proxy_url(proxy_url: str) -> Optional[ClientProxySettings]:
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"

    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        logger.warning(f"Ignoring unsupported proxy scheme: {scheme}")
        return None

    host = parsed.hostname
    if not host:
        logger.warning("Ignoring proxy URL without host")
        return None

    try:
        port = parsed.port
    except ValueError as e:
        logger.warning(f"Ignoring proxy URL with invalid port: {e}")
        return None

    if port is None:
        port = _default_port_for_scheme(scheme)
    elif port <= 0:
        logger.warning(f"Ignoring proxy URL with invalid port: {port}")
        return None

    if port is None:
        logger.warning(f"Ignoring unsupported proxy scheme: {scheme}")
        return None

    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    if scheme == "http":
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


def build_tdlib_proxy_settings(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[ClientProxySettings]:
    """
    Build TDLib proxy settings from environment variables.

    Supported proxy URL schemes:
    - http://host:port
    - socks5://host:port
    - socks5h://host:port
    - mtproto://host:port?secret=...
    """
    return build_tdlib_proxy_settings_kwargs(env).get("proxy_settings")


def build_tdlib_proxy_settings_kwargs(
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Optional[ClientProxySettings]]:
    """
    Build the ClientSettings kwargs needed for proxy configuration.

    Returning an empty dict preserves aiotdlib's native AIOTDLIB_* environment
    parsing. Returning {"proxy_settings": None} explicitly disables proxies.
    """
    if env is None:
        env = os.environ

    app_proxy_url = _get_env(env, APP_PROXY_ENV)
    if app_proxy_url:
        proxy_settings = _parse_proxy_url(app_proxy_url)
        if proxy_settings is not None:
            return {"proxy_settings": proxy_settings}

    if _no_proxy_disables_all(env):
        return {"proxy_settings": None}

    for key in FALLBACK_PROXY_ENV_KEYS:
        proxy_url = _get_env(env, key)
        if not proxy_url:
            continue

        proxy_settings = _parse_proxy_url(proxy_url)
        if proxy_settings is not None:
            return {"proxy_settings": proxy_settings}

    return {}
