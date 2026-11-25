"""Outbound calls to guardian (feed) and ops-bot (approval requests, founder notifications)."""
from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .log import log


class Outbox:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client

    def _headers(self) -> dict[str, str]:
        # Both headers while receivers migrate from X-FestiveKit-Token to X-Insidious-Token.
        tok = self.s.internal_token
        return {"X-Insidious-Token": tok, "X-FestiveKit-Token": tok} if tok else {}

    async def feed(self, text: str, source: str = "agent") -> bool:
        """Best effort: guardian's cockpit feed. Never raises."""
        if not self.s.guardian_url:
            return False
        try:
            r = await self.client.post(f"{self.s.guardian_url}/api/feed", json={"source": source, "text": text[:500]},
                                       headers=self._headers(), timeout=2.0)
            return r.status_code < 400
        except httpx.HTTPError as e:
            log("feed_failed", error=type(e).__name__)
            return False

    async def notify(self, text_en: str, severity: str = "info", voice: bool = True) -> dict[str, Any]:
        """POST ops-bot /nb/notify. Raises httpx.HTTPError / RuntimeError on failure."""
        r = await self.client.post(f"{self.s.ops_bot_url}/nb/notify",
                                   json={"text_en": text_en, "severity": severity, "voice": voice},
                                   headers=self._headers(), timeout=10.0)
        if r.status_code >= 400:
            raise RuntimeError(f"ops-bot /nb/notify -> HTTP {r.status_code}")
        return r.json() if r.content else {"ok": True}

    async def notify_quiet(self, text_en: str, severity: str = "info", voice: bool = True) -> bool:
        try:
            await self.notify(text_en, severity, voice)
            return True
        except Exception as e:  # noqa: BLE001
            log("notify_failed", error=f"{type(e).__name__}: {str(e)[:150]}")
            return False

    async def approval_request(self, payload: dict) -> dict[str, Any]:
        try:
            r = await self.client.post(f"{self.s.ops_bot_url}/nb/approval-request", json=payload,
                                       headers=self._headers(), timeout=10.0)
        except httpx.HTTPError as e:
            log("approval_request_failed", plan_id=payload.get("plan_id"), error=type(e).__name__)
            return {"ok": False, "error": type(e).__name__}
        if r.status_code >= 400:
            log("approval_request_failed", plan_id=payload.get("plan_id"), status=r.status_code)
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        body = r.json() if r.content else {}
        return {"ok": True, "pending_id": body.get("pending_id")}
