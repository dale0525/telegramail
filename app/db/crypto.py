"""Secret encryption helpers for the v2 database."""

import base64
import binascii
import os
from pathlib import Path
from typing import Tuple


class MasterKeyError(RuntimeError):
    """Raised for invalid or unavailable key material without exposing it."""


def _decode_master_key(material: bytes, *, allow_raw: bool = False) -> bytes:
    if allow_raw and len(material) == 32:
        return material
    try:
        decoded = base64.b64decode(b"".join(material.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError("master key is not valid") from exc
    if len(decoded) != 32:
        raise MasterKeyError("master key is not valid")
    return decoded


def load_master_key(path: str | None = None) -> bytes:
    """Load a 32-byte AES key, preferring direct environment configuration.

    ``MASTER_KEY`` is a base64-encoded key intended for the deployment `.env`.
    ``MASTER_KEY_FILE`` remains a read-only fallback solely for legacy v1→v2
    migrations and restores; it is not required by normal container startup.
    An explicit ``path`` always means file input for those migration callers.
    """
    if path is None:
        configured = os.getenv("MASTER_KEY") or os.getenv("TELEGRAMAIL_MASTER_KEY")
        if configured:
            return _decode_master_key(configured.encode("ascii"))
    key_path = path or os.getenv("MASTER_KEY_FILE") or os.getenv("TELEGRAMAIL_MASTER_KEY_FILE")
    if not key_path:
        raise MasterKeyError("MASTER_KEY is not configured")
    try:
        material = Path(key_path).read_bytes()
    except OSError as exc:
        raise MasterKeyError("legacy master key file cannot be read") from exc
    return _decode_master_key(material, allow_raw=True)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised by deployment setup
        raise MasterKeyError("AES-GCM support is unavailable") from exc
    return AESGCM


def encrypt_secret(value: str | bytes, key: bytes, *, aad: bytes = b"") -> Tuple[bytes, bytes]:
    """Return a fresh 96-bit nonce and AES-256-GCM ciphertext (including tag)."""
    if len(key) != 32:
        raise MasterKeyError("master key is not valid")
    plaintext = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    nonce = os.urandom(12)
    return nonce, _aesgcm()(key).encrypt(nonce, plaintext, aad)


def decrypt_secret(nonce: bytes, ciphertext: bytes, key: bytes, *, aad: bytes = b"") -> bytes:
    """Decrypt an AES-256-GCM value, propagating authentication failures."""
    if len(key) != 32 or len(nonce) != 12:
        raise MasterKeyError("encrypted secret is not valid")
    return _aesgcm()(key).decrypt(bytes(nonce), bytes(ciphertext), aad)
