#!/usr/bin/env python3
"""Container startup gate for the TelegramMail v2 ASGI process.

Schema changes are intentionally never applied here.  Deployments must run the
reviewed migration command explicitly; this entrypoint only verifies that the
database is already at the expected schema before accepting traffic.
"""

from __future__ import annotations

import os
import base64
import binascii
import subprocess
import sys
from pathlib import Path



def _configuration_error(message: str) -> "None":
    raise SystemExit(f"Configuration error: {message}")


def _validate_runtime_paths() -> None:
    data_dir = Path(os.environ.get("TELEGRAMAIL_DATA_DIR", "/app/data"))
    if not data_dir.is_dir():
        _configuration_error("TELEGRAMAIL_DATA_DIR must be an existing directory")
    if not os.access(data_dir, os.W_OK):
        _configuration_error("TELEGRAMAIL_DATA_DIR must be writable")

    direct_key = os.environ.get("MASTER_KEY") or os.environ.get("TELEGRAMAIL_MASTER_KEY")
    if direct_key:
        try:
            valid_direct_key = len(base64.b64decode(direct_key.encode("ascii"), validate=True)) == 32
        except (UnicodeEncodeError, binascii.Error, ValueError):
            valid_direct_key = False
        if not valid_direct_key:
            _configuration_error("MASTER_KEY must be a base64-encoded 32-byte key")
    else:
        # Migration/restore compatibility only.  Normal Compose deployments use
        # MASTER_KEY directly and no longer mount a secret file.
        key_file = os.environ.get("MASTER_KEY_FILE") or os.environ.get("TELEGRAMAIL_MASTER_KEY_FILE")
        if not key_file:
            _configuration_error("MASTER_KEY is required")
        if not Path(key_file).is_file() or not os.access(key_file, os.R_OK):
            _configuration_error("legacy master key file must be readable")


def _check_migrations() -> None:
    migration_script = Path(__file__).with_name("migrate_v2.py")
    if not migration_script.is_file():
        _configuration_error("migration gate is unavailable (scripts/migrate_v2.py is missing)")
    subprocess.run(
        [sys.executable, str(migration_script), "--check-only"],
        check=True,
    )


def main() -> None:
    _validate_runtime_paths()
    _check_migrations()
    os.execvp(sys.executable, [sys.executable, "-m", "app.main"])


if __name__ == "__main__":
    main()
