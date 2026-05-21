import base64
import ipaddress
import os
import socket
import ssl
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlparse

from app.utils.logger import Logger

logger = Logger().get_logger(__name__)

MAIL_PROXY_ENV = "TELEGRAMAIL_MAIL_PROXY"
FALLBACK_PROXY_ENV_KEYS = (
    "all_proxy",
    "ALL_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
)
NO_PROXY_ENV_KEYS = ("no_proxy", "NO_PROXY")
SUPPORTED_MAIL_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


@dataclass(frozen=True)
class MailProxyConfig:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def _get_env(env: Mapping[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return None

    value = value.strip()
    return value or None


def _default_port_for_scheme(scheme: str) -> Optional[int]:
    return {
        "http": 80,
        "https": 443,
        "socks5": 1080,
        "socks5h": 1080,
    }.get(scheme)


def _parse_proxy_url(proxy_url: str) -> Optional[MailProxyConfig]:
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"

    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in SUPPORTED_MAIL_PROXY_SCHEMES:
        logger.warning(f"Ignoring unsupported mail proxy scheme: {scheme}")
        return None

    host = parsed.hostname
    if not host:
        logger.warning("Ignoring mail proxy URL without host")
        return None

    try:
        port = parsed.port
    except ValueError as e:
        logger.warning(f"Ignoring mail proxy URL with invalid port: {e}")
        return None

    if port is None:
        port = _default_port_for_scheme(scheme)

    if port is None or port <= 0 or port > 65535:
        logger.warning(f"Ignoring mail proxy URL with invalid port: {port}")
        return None

    return MailProxyConfig(
        scheme=scheme,
        host=host,
        port=port,
        username=unquote(parsed.username) if parsed.username is not None else "",
        password=unquote(parsed.password) if parsed.password is not None else "",
    )


def _no_proxy_values(env: Mapping[str, str]) -> list[str]:
    values: list[str] = []
    for key in NO_PROXY_ENV_KEYS:
        raw = _get_env(env, key)
        if raw:
            values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def _split_no_proxy_host_port(pattern: str) -> tuple[str, Optional[int]]:
    if pattern.startswith("["):
        end = pattern.find("]")
        if end == -1:
            return pattern, None
        host = pattern[1:end]
        rest = pattern[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None

    if pattern.count(":") == 1:
        host, port_raw = pattern.rsplit(":", 1)
        if port_raw.isdigit():
            return host, int(port_raw)

    return pattern, None


def _host_matches_no_proxy(
    target_host: str,
    target_port: Optional[int],
    pattern: str,
) -> bool:
    pattern = pattern.strip().lower()
    if not pattern:
        return False

    if pattern == "*":
        return True

    host_pattern, port_pattern = _split_no_proxy_host_port(pattern)
    if (
        port_pattern is not None
        and target_port is not None
        and port_pattern != target_port
    ):
        return False

    host_pattern = host_pattern.strip().lower().strip("[]")
    target = (target_host or "").strip().lower().strip("[]")
    if not host_pattern or not target:
        return False

    if host_pattern.startswith("*."):
        suffix = host_pattern[1:]
        return target.endswith(suffix) and target != host_pattern[2:]

    if host_pattern.startswith("."):
        suffix = host_pattern[1:]
        return target == suffix or target.endswith(host_pattern)

    return target == host_pattern or target.endswith(f".{host_pattern}")


def _no_proxy_matches_target(
    target_host: Optional[str],
    target_port: Optional[int],
    env: Mapping[str, str],
) -> bool:
    if not target_host:
        return False

    return any(
        _host_matches_no_proxy(target_host, target_port, pattern)
        for pattern in _no_proxy_values(env)
    )


def build_mail_proxy_config(
    *,
    target_host: Optional[str] = None,
    target_port: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[MailProxyConfig]:
    """
    Build raw mail protocol proxy settings from environment variables.

    IMAP and SMTP do not honor HTTP proxy environment variables themselves, so
    the app creates the proxy tunnel before handing the socket to stdlib clients.
    """
    if env is None:
        env = os.environ

    app_proxy_url = _get_env(env, MAIL_PROXY_ENV)
    if app_proxy_url:
        proxy_config = _parse_proxy_url(app_proxy_url)
        if proxy_config is not None:
            return proxy_config
        logger.warning(
            f"Ignoring fallback proxy environment variables because {MAIL_PROXY_ENV} is invalid"
        )
        return None

    if _no_proxy_matches_target(target_host, target_port, env):
        return None

    for key in FALLBACK_PROXY_ENV_KEYS:
        proxy_url = _get_env(env, key)
        if not proxy_url:
            continue

        proxy_config = _parse_proxy_url(proxy_url)
        if proxy_config is not None:
            return proxy_config

    return None


def create_proxied_socket(
    target_host: str,
    target_port: int,
    proxy_config: MailProxyConfig,
    timeout: Any = None,
    source_address: Optional[tuple[str, int]] = None,
) -> socket.socket:
    if proxy_config.scheme in {"http", "https"}:
        return _create_http_connect_socket(
            target_host,
            target_port,
            proxy_config,
            timeout,
            source_address,
        )

    if proxy_config.scheme in {"socks5", "socks5h"}:
        return _create_socks5_socket(
            target_host,
            target_port,
            proxy_config,
            timeout,
            source_address,
        )

    raise OSError(f"Unsupported mail proxy scheme: {proxy_config.scheme}")


def _create_proxy_socket(
    proxy_config: MailProxyConfig,
    timeout: Any,
    source_address: Optional[tuple[str, int]],
) -> socket.socket:
    address = (proxy_config.host, proxy_config.port)
    if source_address is not None:
        return socket.create_connection(
            address,
            timeout=timeout,
            source_address=source_address,
        )

    return socket.create_connection(address, timeout=timeout)


def _wrap_https_proxy_socket(
    sock: socket.socket,
    proxy_config: MailProxyConfig,
) -> socket.socket:
    context = ssl.create_default_context()
    try:
        return context.wrap_socket(sock, server_hostname=proxy_config.host)
    except Exception:
        sock.close()
        raise


def _create_http_connect_socket(
    target_host: str,
    target_port: int,
    proxy_config: MailProxyConfig,
    timeout: Any,
    source_address: Optional[tuple[str, int]],
) -> socket.socket:
    sock = _create_proxy_socket(proxy_config, timeout, source_address)
    if proxy_config.scheme == "https":
        sock = _wrap_https_proxy_socket(sock, proxy_config)

    try:
        _send_http_connect(sock, proxy_config, target_host, target_port)
        return sock
    except Exception:
        sock.close()
        raise


def _authority(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _send_http_connect(
    sock: socket.socket,
    proxy_config: MailProxyConfig,
    target_host: str,
    target_port: int,
) -> None:
    authority = _authority(target_host, target_port)
    headers = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: Keep-Alive",
    ]

    if proxy_config.username or proxy_config.password:
        credentials = f"{proxy_config.username}:{proxy_config.password}".encode("utf-8")
        token = base64.b64encode(credentials).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")

    request = "\r\n".join(headers) + "\r\n\r\n"
    sock.sendall(request.encode("ascii"))

    response = _read_http_response_header(sock)
    status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split(None, 2)
    if len(parts) < 2:
        raise OSError(f"Invalid HTTP proxy CONNECT response: {status_line}")

    try:
        status_code = int(parts[1])
    except ValueError as e:
        raise OSError(f"Invalid HTTP proxy CONNECT status: {status_line}") from e

    if status_code != 200:
        reason = parts[2] if len(parts) > 2 else ""
        suffix = f" {reason}" if reason else ""
        raise OSError(f"HTTP proxy CONNECT failed with status {status_code}{suffix}")


def _read_http_response_header(sock: socket.socket) -> bytes:
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
        if len(response) > 65536:
            raise OSError("HTTP proxy CONNECT response headers are too large")

    if b"\r\n\r\n" not in response:
        raise OSError("HTTP proxy CONNECT response ended before headers completed")

    return response.split(b"\r\n\r\n", 1)[0]


def _create_socks5_socket(
    target_host: str,
    target_port: int,
    proxy_config: MailProxyConfig,
    timeout: Any,
    source_address: Optional[tuple[str, int]],
) -> socket.socket:
    sock = _create_proxy_socket(proxy_config, timeout, source_address)
    try:
        _send_socks5_connect(sock, proxy_config, target_host, target_port)
        return sock
    except Exception:
        sock.close()
        raise


def _send_socks5_connect(
    sock: socket.socket,
    proxy_config: MailProxyConfig,
    target_host: str,
    target_port: int,
) -> None:
    methods = [0x00]
    if proxy_config.username or proxy_config.password:
        methods.append(0x02)

    sock.sendall(bytes([0x05, len(methods), *methods]))
    version, method = _recv_exact(sock, 2)
    if version != 0x05:
        raise OSError("Invalid SOCKS5 proxy greeting response")
    if method == 0xFF:
        raise OSError("SOCKS5 proxy does not accept supported authentication methods")

    if method == 0x02:
        _send_socks5_username_password_auth(sock, proxy_config)

    request = b"\x05\x01\x00" + _socks5_address_bytes(target_host) + struct.pack(
        "!H",
        int(target_port),
    )
    sock.sendall(request)

    header = _recv_exact(sock, 4)
    if header[0] != 0x05:
        raise OSError("Invalid SOCKS5 proxy connect response")
    if header[1] != 0x00:
        raise OSError(f"SOCKS5 proxy connect failed with reply code {header[1]}")

    _consume_socks5_bound_address(sock, header[3])


def _send_socks5_username_password_auth(
    sock: socket.socket,
    proxy_config: MailProxyConfig,
) -> None:
    username = proxy_config.username.encode("utf-8")
    password = proxy_config.password.encode("utf-8")
    if len(username) > 255 or len(password) > 255:
        raise OSError("SOCKS5 username/password is too long")

    sock.sendall(
        bytes([0x01, len(username)]) + username + bytes([len(password)]) + password
    )
    response = _recv_exact(sock, 2)
    if response[0] != 0x01 or response[1] != 0x00:
        raise OSError("SOCKS5 username/password authentication failed")


def _socks5_address_bytes(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        encoded_host = host.encode("idna")
        if len(encoded_host) > 255:
            raise OSError("SOCKS5 target hostname is too long")
        return bytes([0x03, len(encoded_host)]) + encoded_host

    if ip.version == 4:
        return b"\x01" + ip.packed

    return b"\x04" + ip.packed


def _consume_socks5_bound_address(sock: socket.socket, address_type: int) -> None:
    if address_type == 0x01:
        _recv_exact(sock, 4)
    elif address_type == 0x03:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    elif address_type == 0x04:
        _recv_exact(sock, 16)
    else:
        raise OSError(f"Invalid SOCKS5 bound address type: {address_type}")

    _recv_exact(sock, 2)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("Proxy connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
