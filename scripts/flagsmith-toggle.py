#!/usr/bin/env python3
"""Read/flip Flagsmith environment-level flags through the Admin API (stdlib only).

Uses the same legacy featurestate endpoints as the NudgeBee HTTP tasks (docs/CONTRACTS.md), so it is
also the quickest way to prove those calls work before wiring them into a workflow.

  python3 scripts/flagsmith-toggle.py ids                       # env-level featurestate ids (paste into NudgeBee)
  python3 scripts/flagsmith-toggle.py get heavy_features
  python3 scripts/flagsmith-toggle.py set heavy_features=off
  python3 scripts/flagsmith-toggle.py set heavy_features=on payment_gateway=razorpay
  python3 scripts/flagsmith-toggle.py --dry-run set heavy_features=off   # print requests, send nothing

Env (or .env): FLAGSMITH_ADMIN_URL, FLAGSMITH_ENVIRONMENT_KEY, FLAGSMITH_ORG_API_KEY, FLAGSMITH_PROJECT_ID (optional).
Paths are unverified against the live Swagger (https://api.flagsmith.com/api/v1/docs/) and only work
while Feature Versioning v2 is OFF on the environment.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import ROOT, parse  # noqa: E402

DOTENV = parse(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key) or DOTENV.get(key) or default


ADMIN = env("FLAGSMITH_ADMIN_URL", "https://api.flagsmith.com/api/v1").rstrip("/")
ENV_KEY = env("FLAGSMITH_ENVIRONMENT_KEY")
ORG_KEY = env("FLAGSMITH_ORG_API_KEY")
PROJECT_ID = env("FLAGSMITH_PROJECT_ID")
DRY = False


def flag_names() -> list[str]:
    text = (ROOT / "flagsmith" / "flags.yaml").read_text(encoding="utf-8")
    block = text.split("segments:", 1)[0]
    return re.findall(r"^\s*-\s*name:\s*([A-Za-z0-9_]+)", block, flags=re.M)


def call(method: str, path: str, body: dict | None = None):
    url = f"{ADMIN}{path}"
    data = json.dumps(body).encode() if body is not None else None
    if DRY and method != "GET":
        print(f"[dry-run] {method} {url} {json.dumps(body) if body else ''}")
        return None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Api-Key {ORG_KEY}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {detail}")


def results(payload) -> list[dict]:
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    return payload if isinstance(payload, list) else []


_feature_ids: dict[str, int] = {}


def feature_id(name: str) -> int | None:
    if not PROJECT_ID:
        return None
    if name not in _feature_ids:
        q = urllib.parse.urlencode({"search": name})
        for f in results(call("GET", f"/projects/{PROJECT_ID}/features/?{q}")):
            if f.get("name") == name:
                _feature_ids[name] = f["id"]
    return _feature_ids.get(name)


def env_featurestate(name: str) -> dict:
    q = urllib.parse.urlencode({"feature_name": name})
    rows = results(call("GET", f"/environments/{ENV_KEY}/featurestates/?{q}"))
    rows = [r for r in rows if not r.get("identity") and not r.get("feature_segment")]
    fid = feature_id(name)
    if fid is not None:
        rows = [r for r in rows if (r.get("feature") if not isinstance(r.get("feature"), dict) else r["feature"].get("id")) == fid]
    if len(rows) != 1:
        raise SystemExit(f"{name}: expected 1 environment-level featurestate, got {len(rows)} "
                         f"(set FLAGSMITH_PROJECT_ID to disambiguate)")
    return rows[0]


def parse_assignment(arg: str) -> tuple[str, dict]:
    if "=" not in arg:
        raise SystemExit(f"bad assignment '{arg}', want name=on|off|<value>")
    name, value = arg.split("=", 1)
    v = value.strip().lower()
    if v in ("on", "true", "enabled", "1"):
        return name, {"enabled": True}
    if v in ("off", "false", "disabled", "0"):
        return name, {"enabled": False}
    return name, {"feature_state_value": value}


def main(argv: list[str]) -> int:
    global DRY
    args = [a for a in argv[1:] if a != "--dry-run"]
    DRY = "--dry-run" in argv
    if not args:
        print(__doc__)
        return 2
    missing = [k for k, v in (("FLAGSMITH_ENVIRONMENT_KEY", ENV_KEY), ("FLAGSMITH_ORG_API_KEY", ORG_KEY)) if not v or v.startswith("xxxx")]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} (env or .env)")

    cmd, rest = args[0], args[1:]
    if cmd == "ids":
        print(f"{'flag':22} {'featurestate_id':>16}  enabled  value")
        for name in flag_names():
            try:
                fs = env_featurestate(name)
                print(f"{name:22} {fs.get('id'):>16}  {str(fs.get('enabled')):7}  {fs.get('feature_state_value')}")
            except SystemExit as e:
                print(f"{name:22} {'?':>16}  {e}")
        print(f"\nPATCH {ADMIN}/environments/{ENV_KEY}/featurestates/<id>/  body {{\"enabled\": false}}")
        return 0
    if cmd == "get":
        for name in rest:
            print(json.dumps(env_featurestate(name), indent=2))
        return 0
    if cmd == "set":
        for arg in rest:
            name, body = parse_assignment(arg)
            fs = env_featurestate(name)
            call("PATCH", f"/environments/{ENV_KEY}/featurestates/{fs['id']}/", body)
            if not DRY:
                print(f"{name}: {json.dumps(body)} (featurestate {fs['id']})")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
