#!/usr/bin/env python3
"""Exit non-zero unless the local readiness endpoint reports HTTP 2xx."""

from __future__ import annotations

import sys
import os
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    port = os.getenv("PORT", "8080")
    try:
        # A deployment may intentionally retain outbound HTTP(S) proxy settings
        # for provider access. Readiness is process-local and must never leave the
        # container or depend on NO_PROXY being configured correctly.
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health/ready", timeout=3) as response:
            return 0 if 200 <= response.status < 300 else 1
    except (OSError, URLError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
