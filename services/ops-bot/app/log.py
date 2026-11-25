"""One JSON line per event on stdout. Never pass secrets in here."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

SVC = "ops-bot"


def log(event: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "svc": SVC, "event": event}
    for k, v in fields.items():
        if isinstance(v, (bytes, bytearray)):
            v = f"<{len(v)} bytes>"
        rec[k] = v
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover
        line = json.dumps({"ts": rec["ts"], "svc": SVC, "event": event, "note": "unserialisable fields"})
    print(line, file=sys.stdout, flush=True)
