"""Flagsmith flag toggles for the /ops cockpit (environment-level featurestates via the Admin API).

Same legacy endpoints as services/agent/app/flagsmith_admin.py (copied, not imported: services stay independent):

  GET   /projects/{project}/features/                       names + descriptions (project from FLAGSMITH_PROJECT_ID
                                                              or GET /environments/{env_key}/ -> "project")
  GET   /environments/{env_key}/featurestates/               states; environment-level = no identity, no feature_segment
  GET   /environments/{env_key}/featurestates/?feature_name= one feature's states (before a write)
  PATCH /environments/{env_key}/featurestates/{id}/          {"enabled": bool} and/or {"feature_state_value": "..."}

Header `Authorization: Api-Key $FLAGSMITH_ORG_API_KEY`. Works only while Feature Versioning v2 is OFF.
The store itself keeps reading flags through local evaluation, so a write shows up within FLAGSMITH_REFRESH_SECONDS.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_ADMIN_URL = "https://api.flagsmith.com/api/v1"

# Cockpit grouping (order matters: groups and flags render in this order).
GROUPS: list[tuple[str, list[str]]] = [
    ("Load shedding", ["heavy_features", "recommendations", "hd_images", "live_reviews"]),
    ("Checkout", ["payment_gateway", "checkout_flow"]),
    ("Database", ["read_from_replica", "db_pool", "db_read_url"]),
    ("AI agent", ["ai_agent", "agent_config"]),
    ("Founder voice", ["ops_voice"]),
]
GROUP_ORDER = [g for g, _ in GROUPS] + ["Other"]
_GROUP_OF = {name: g for g, names in GROUPS for name in names}
_ORDER_OF = {name: i for i, name in enumerate(n for _, names in GROUPS for n in names)}

PARENT = "heavy_features"
CHILDREN = {"recommendations", "hd_images", "live_reviews"}
# Remote-config values the cockpit may edit, with the allowed choices.
VALUE_OPTIONS: dict[str, list[str]] = {"payment_gateway": ["razorpay", "cashfree"]}
MASK = "••••••"
_PROJECTS: dict[tuple[str, str], str] = {}  # (admin url, env key) -> project id, resolved once


def group_of(name: str) -> str:
    return _GROUP_OF.get(name, "Other")


def is_sensitive(name: str) -> bool:
    n = name.lower()
    return n == "db_read_url" or "url" in n or "password" in n


def _mask_url(raw: str) -> str:
    """scheme://user:***@host:port/path: the host stays visible; password, query and fragment do not."""
    try:
        u = urlsplit(raw)
        if not u.scheme or not u.hostname:
            return MASK
        user = f"{u.username}:***@" if u.username and u.password else (f"{u.username}@" if u.username else "")
        port = f":{u.port}" if u.port else ""
        tail = "?…" if u.query or u.fragment else ""
        return f"{u.scheme}://{user}{u.hostname}{port}{u.path}{tail}"
    except ValueError:
        return MASK


def mask_value(name: str, value: Any) -> Any:
    if not is_sensitive(name) or value in (None, ""):
        return value
    if "password" not in name.lower() and "://" in str(value):
        return _mask_url(str(value))
    return MASK


def value_editable(name: str) -> bool:
    return name in VALUE_OPTIONS and not is_sensitive(name)


def describe(name: str, enabled: bool, value: Any, description: str, kind: str) -> dict:
    row = {
        "name": name,
        "enabled": bool(enabled),
        "value": mask_value(name, value),
        "masked": is_sensitive(name) and value not in (None, ""),
        "description": description or "",
        "group": group_of(name),
        "kind": kind,  # "flag" (on/off) | "config" (remote config value)
        "parent": PARENT if name in CHILDREN else None,
        "is_parent": name == PARENT,
    }
    if value_editable(name):
        row["options"] = list(VALUE_OPTIONS[name])
    return row


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (GROUP_ORDER.index(r["group"]), _ORDER_OF.get(r["name"], 999), r["name"]))


def kind_of(spec_type: str | None, value: Any) -> str:
    t = str(spec_type or "").upper()
    if t in {"CONFIG", "MULTIVARIATE"}:
        return "config"
    if t == "FLAG":
        return "flag"
    return "config" if value not in (None, "") else "flag"  # unknown / STANDARD: a value means remote config


class FlagsmithError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FlagsmithAdmin:
    """Tiny async Admin API client. Config is read from the environment at construction time."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str | None = None, api_key: str | None = None,
                 env_key: str | None = None, project_id: str | None = None, timeout: float = 8.0) -> None:
        self.base_url = (base_url if base_url is not None
                         else os.environ.get("FLAGSMITH_ADMIN_URL") or DEFAULT_ADMIN_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("FLAGSMITH_ORG_API_KEY", "")
        self.env_key = env_key if env_key is not None else os.environ.get("FLAGSMITH_ENVIRONMENT_KEY", "")
        self.project_id = project_id if project_id is not None else os.environ.get("FLAGSMITH_PROJECT_ID", "")
        self.client = client
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.env_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Api-Key {self.api_key}", "Content-Type": "application/json"}

    async def _call(self, method: str, url: str, *, params: dict | None = None, body: Any = None) -> Any:
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"
        try:
            r = await self.client.request(method, url, params=params, json=body, headers=self._headers(),
                                          timeout=self.timeout)
        except httpx.HTTPError as e:
            raise FlagsmithError(f"{method} {url.split('?')[0]}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise FlagsmithError(f"Flagsmith {method} -> HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    async def _list(self, path: str, params: dict | None = None) -> list[dict]:
        rows: list[dict] = []
        data = await self._call("GET", path, params=params)
        for _ in range(20):  # pagination guard
            if isinstance(data, list):
                rows.extend(data)
                break
            if not isinstance(data, dict):
                break
            rows.extend(data.get("results") or [])
            if not data.get("next"):
                break
            data = await self._call("GET", data["next"])
        return rows

    async def _project(self) -> str:
        if not self.project_id:
            self.project_id = _PROJECTS.get((self.base_url, self.env_key), "")
        if not self.project_id:
            env = await self._call("GET", f"/environments/{self.env_key}/")
            proj = env.get("project") if isinstance(env, dict) else None
            if isinstance(proj, dict):
                proj = proj.get("id")
            if proj in (None, ""):
                raise FlagsmithError("cannot resolve the Flagsmith project for this environment")
            self.project_id = _PROJECTS[(self.base_url, self.env_key)] = str(proj)
        return self.project_id

    async def list_features(self) -> list[dict]:
        return await self._list(f"/projects/{await self._project()}/features/", {"page_size": 100})

    async def environment_states(self) -> list[dict]:
        rows = await self._list(f"/environments/{self.env_key}/featurestates/", {"page_size": 200})
        return [fs for fs in rows if _env_level(fs)]

    async def environment_featurestate(self, name: str) -> dict | None:
        rows = await self._list(f"/environments/{self.env_key}/featurestates/", {"feature_name": name})
        env_rows = [fs for fs in rows if _env_level(fs)]
        if len(env_rows) <= 1:
            return env_rows[0] if env_rows else None
        # The filter was not applied (several features came back): never patch the wrong one, match by feature id.
        fid = next((f.get("id") for f in await self.list_features() if f.get("name") == name), None)
        return next((fs for fs in env_rows if fid is not None and _feature_id(fs) == fid), None)

    async def set_state(self, name: str, body: dict) -> dict:
        fs = await self.environment_featurestate(name)
        if not fs or fs.get("id") is None:
            raise FlagsmithError(f"no environment-level feature state for {name!r} (does the feature exist?)", 404)
        out = await self._call("PATCH", f"/environments/{self.env_key}/featurestates/{fs['id']}/", body=body)
        return out if isinstance(out, dict) else {}


def _env_level(fs: dict) -> bool:
    return fs.get("identity") in (None, "") and fs.get("feature_segment") in (None, "")


def _feature_id(fs: dict) -> Any:
    f = fs.get("feature")
    return f.get("id") if isinstance(f, dict) else f


def raw_value(fs: dict) -> Any:
    """feature_state_value is a plain value on the legacy serializers; tolerate the typed dict form too."""
    v = fs.get("feature_state_value")
    if isinstance(v, dict):
        for k in ("string_value", "integer_value", "boolean_value"):
            if v.get(k) is not None:
                return v[k]
        return None
    return v


def merge(features: list[dict], states: list[dict], specs: dict[str, dict]) -> list[dict]:
    """Join features (name/description) with their environment-level states."""
    by_id = {f.get("id"): f for f in features}
    rows = []
    for fs in states:
        fid = _feature_id(fs)
        feat = by_id.get(fid) or (fs.get("feature") if isinstance(fs.get("feature"), dict) else None)
        if not feat or not feat.get("name"):
            continue
        name = feat["name"]
        spec = specs.get(name, {})
        value = raw_value(fs)
        ftype = spec.get("type") or feat.get("type")
        rows.append(describe(name, bool(fs.get("enabled")), value,
                             feat.get("description") or spec.get("description") or "", kind_of(ftype, value)))
    return sort_rows(rows)
