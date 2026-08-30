"""SQLite v2 persistence API.

This package deliberately does not import the legacy ``app.database`` module.
New HTTP handlers and workers should share one :class:`V2Repository` instance
per process, while each operation uses its own SQLite connection.
"""

from .crypto import MasterKeyError, decrypt_secret, encrypt_secret, load_master_key
from .repository import Database, V2Repository, decode_inbox_cursor, encode_inbox_cursor
from .schema import CURRENT_SCHEMA_VERSION, initialize_schema

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "Database",
    "MasterKeyError",
    "V2Repository",
    "decode_inbox_cursor",
    "encode_inbox_cursor",
    "decrypt_secret",
    "encrypt_secret",
    "initialize_schema",
    "load_master_key",
]
