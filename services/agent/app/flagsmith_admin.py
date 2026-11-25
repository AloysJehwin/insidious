"""Flagsmith Admin API writes (environment-level featurestates). Same endpoints as cli/insidious_cli/flagsmith_admin.py.

  GET   /environments/{env_key}/featurestates/?feature_name=<name>   -> find the env-level state (no identity/segment)
  PATCH /environments/{env_key}/featurestates/{id}/                  {"enabled": bool} | {"feature_state_value": v}

Header `Authorization: Api-Key $FLAGSMITH_ORG_API_KEY`. Works only while Feature Versioning v2 is OFF.
"""
from __future__ import annotations

from typing import Any

import httpx


class FlagsmithError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FlagsmithAdmin:
    def __init__(self, base_url: str, api_key: str, env_key: str, client: httpx.AsyncClient, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.env_key = env_key
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

    async def environment_featurestate(self, name: str) -> dict | None:
        data = await self._call("GET", f"/environments/{self.env_key}/featurestates/", params={"feature_name": name})
        rows: list[dict] = []
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
        for fs in rows:
            if fs.get("identity") in (None, "") and fs.get("feature_segment") in (None, ""):
                return fs
        return None

    async def set_state(self, name: str, body: dict) -> dict:
        fs = await self.environment_featurestate(name)
        if not fs or fs.get("id") is None:
            raise FlagsmithError(f"no environment-level feature state for {name!r} (does the feature exist?)")
        return await self._call("PATCH", f"/environments/{self.env_key}/featurestates/{fs['id']}/", body=body)

    async def set_flag(self, name: str, enabled: bool) -> dict:
        return await self.set_state(name, {"enabled": bool(enabled)})

    async def set_value(self, name: str, value: Any) -> dict:
        return await self.set_state(name, {"feature_state_value": value})
