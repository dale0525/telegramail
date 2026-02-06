import select
from typing import Any, Callable, Iterable

from app.utils import Logger

logger = Logger().get_logger(__name__)

WaitForDataFn = Callable[[Any, int], bool]


def _to_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="ignore")
    return str(value).encode("utf-8", errors="ignore")


def _capability_tokens(capability_data: Iterable[Any]) -> set[str]:
    tokens: set[str] = set()
    for line in capability_data or []:
        raw = _to_bytes(line).decode("utf-8", errors="ignore").upper()
        for token in raw.split():
            token = token.strip()
            if token:
                tokens.add(token)
    return tokens


def supports_idle(conn: Any) -> bool:
    try:
        status, capability_data = conn.capability()
    except Exception as e:
        logger.warning(f"Failed to fetch IMAP capability: {e}")
        return False

    if status != "OK":
        return False

    return "IDLE" in _capability_tokens(capability_data)


def _default_wait_for_data(conn: Any, timeout_seconds: int) -> bool:
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    readable, _, _ = select.select([sock], [], [], max(timeout_seconds, 0))
    return bool(readable)


def _readline_when_ready(
    conn: Any, timeout_seconds: int, wait_for_data: WaitForDataFn
) -> bytes | None:
    if not wait_for_data(conn, timeout_seconds):
        return None
    try:
        return conn.readline()
    except Exception as e:
        logger.warning(f"Failed to read IMAP line during IDLE: {e}")
        return None


def idle_wait_once(
    conn: Any,
    timeout_seconds: int = 1740,
    wait_for_data: WaitForDataFn | None = None,
) -> bool:
    wait_fn = wait_for_data or _default_wait_for_data
    changed = False

    tag = _to_bytes(conn._new_tag())
    idle_command = tag + b" IDLE\r\n"
    tagged_prefix = tag + b" "

    conn.send(idle_command)
    entered = _readline_when_ready(conn, timeout_seconds=5, wait_for_data=wait_fn)
    if not entered or not entered.startswith(b"+"):
        return False

    try:
        line = _readline_when_ready(
            conn, timeout_seconds=timeout_seconds, wait_for_data=wait_fn
        )
        if line:
            upper = line.upper()
            changed = b" EXISTS" in upper or b" RECENT" in upper
        return changed
    finally:
        try:
            conn.send(b"DONE\r\n")
        except Exception as e:
            logger.warning(f"Failed to send DONE for IMAP IDLE: {e}")
            return changed

        # Drain the tagged completion for this IDLE cycle.
        for _ in range(3):
            line = _readline_when_ready(conn, timeout_seconds=2, wait_for_data=wait_fn)
            if not line:
                break
            if line.upper().startswith(tagged_prefix.upper()):
                break

    return changed
