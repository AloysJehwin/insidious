"""One JSON line per event on stdout. Never pass secrets in fields."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(event: str, **fields: Any) -> None:
    rec = {"ts": utcnow_iso(), "svc": "guardian", "event": event, **fields}
    try:
        sys.stdout.write(json.dumps(rec, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
