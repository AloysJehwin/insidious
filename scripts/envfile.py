#!/usr/bin/env python3
"""Tiny .env helper for the Makefile and scripts (stdlib only).

  python3 scripts/envfile.py get KEY [DEFAULT]   # value from the real environment, else .env, else DEFAULT
                                                 # (INSIDIOUS_X also tries the legacy FESTIVEKIT_X)
  python3 scripts/envfile.py sanitize [FILE]     # KEY=VALUE lines safe for `kubectl --from-env-file`
  python3 scripts/envfile.py mask URL            # hide ?token=... when printing a URL

Why sanitize: kubectl's --from-env-file keeps inline comments and quotes as part of the value
(`FOUNDER_PHONE=9198...   # E.164` would become the whole string). docker compose and python-dotenv
strip them, kubectl does not.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # inline comment = whitespace followed by '#'
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        out[key] = value
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "get":
        if len(argv) < 3:
            return 2
        key = argv[2]
        default = argv[3] if len(argv) > 3 else ""
        # INSIDIOUS_<X> falls back to the legacy FESTIVEKIT_<X> name (environment first, then .env).
        keys = [key] + (["FESTIVEKIT_" + key[len("INSIDIOUS_"):]] if key.startswith("INSIDIOUS_") else [])
        dotenv = parse(ROOT / ".env")
        val = next((os.environ[k] for k in keys if os.environ.get(k)), None) \
            or next((dotenv[k] for k in keys if dotenv.get(k)), None)
        print(val or default)
        return 0
    if cmd == "sanitize":
        path = Path(argv[2]) if len(argv) > 2 else ROOT / ".env"
        if not path.is_file():
            print(f"envfile: {path} not found", file=sys.stderr)
            return 1
        for k, v in parse(path).items():
            if "\n" in v:
                continue
            print(f"{k}={v}")
        return 0
    if cmd == "mask":
        url = argv[2] if len(argv) > 2 else ""
        print(re.sub(r"(token=)[^&]+", r"\1****", url))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
