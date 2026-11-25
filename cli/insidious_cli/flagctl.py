"""Flip flags during rehearse/reset: Flagsmith Admin API when an org key is configured, else the store's
dev override endpoint (`POST STORE_URL/api/dev/flags`, only when the store runs with INSIDIOUS_DEV=1)."""
from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from .flagsmith_admin import FlagsmithAdmin
from .settings import env_value


class FlagController(Protocol):
    mode: str

    def set_flags(self, values: dict[str, Any]) -> None: ...


class AdminFlagController:
    mode = "flagsmith-admin"

    def __init__(self, admin: FlagsmithAdmin, env_key: str):
        self.admin = admin
        self.env_key = env_key

    def set_flags(self, values: dict[str, Any]) -> None:
        for name, v in values.items():
            if isinstance(v, bool):
                self.admin.toggle_flag(self.env_key, name, v)
            else:
                self.admin.set_value(self.env_key, name, v)


class DevFlagController:
    mode = "store-dev-endpoint"

    def __init__(self, store_url: str, client: httpx.Client, dry_run: bool = False):
        self.store_url = store_url.rstrip("/")
        self.client = client
        self.dry_run = dry_run
        self.calls: list[dict[str, Any]] = []

    def set_flags(self, values: dict[str, Any]) -> None:
        self.calls.append({"method": "POST", "url": f"{self.store_url}/api/dev/flags", "body": values})
        if self.dry_run:
            return
        r = self.client.post(f"{self.store_url}/api/dev/flags", json=values)
        if r.status_code == 404:
            raise RuntimeError("store returned 404 for /api/dev/flags: run the store with INSIDIOUS_DEV=1 "
                               "or configure FLAGSMITH_ORG_API_KEY")
        r.raise_for_status()


def admin_configured() -> bool:
    return all(env_value(n) for n in ("FLAGSMITH_ORG_API_KEY", "FLAGSMITH_ENVIRONMENT_KEY", "FLAGSMITH_PROJECT_ID"))


def make_admin(dry_run: bool = False, client: httpx.Client | None = None) -> FlagsmithAdmin:
    return FlagsmithAdmin(
        base_url=os.getenv("FLAGSMITH_ADMIN_URL") or "https://api.flagsmith.com/api/v1",
        api_key=os.getenv("FLAGSMITH_ORG_API_KEY") or "<FLAGSMITH_ORG_API_KEY>",
        project_id=os.getenv("FLAGSMITH_PROJECT_ID") or "<FLAGSMITH_PROJECT_ID>",
        dry_run=dry_run,
        client=client,
    )


def make_flag_controller(store_url: str, client: httpx.Client, dry_run: bool = False) -> FlagController:
    if admin_configured():
        return AdminFlagController(make_admin(dry_run=dry_run), os.environ["FLAGSMITH_ENVIRONMENT_KEY"])
    return DevFlagController(store_url, client, dry_run=dry_run)


def read_store_flags(store_url: str, client: httpx.Client) -> dict[str, Any] | None:
    try:
        r = client.get(f"{store_url.rstrip('/')}/api/flags")
        if r.status_code == 200 and isinstance(r.json(), dict):
            return r.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def default_flag_values(spec: dict, include_values: bool = True) -> dict[str, Any]:
    """flags.yaml defaults: booleans for FLAG features, plus values for CONFIG/MULTIVARIATE when asked."""
    out: dict[str, Any] = {}
    for f in spec.get("features") or []:
        if f.get("type", "FLAG") == "FLAG":
            out[f["name"]] = bool(f.get("default_enabled", False))
        elif include_values and f.get("default_value") is not None:
            out[f["name"]] = f["default_value"]
    return out
