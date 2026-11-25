"""Founder voice settings from the Flagsmith `ops_voice` remote config on identity founder:<FOUNDER_PHONE>.

JSON fields: speaker, pace (TTS), language (e.g. "ta-IN") and style ("native" | "tanglish"). language/style
rank below the founder's own switch message and above the env defaults (see flows.resolve_voice)."""
from __future__ import annotations

import asyncio
import json
import time

from .config import Settings
from .log import log

DEFAULT_VOICE = {"speaker": "", "pace": 1.0, "language": "", "style": ""}


class VoiceConfig:
    def __init__(self, settings: Settings, ttl_s: float = 15.0):
        self.s = settings
        self.ttl_s = ttl_s
        self._client = None
        self._cache: tuple[float, dict] | None = None

    def _fetch_sync(self) -> dict:
        if self._client is None:
            from flagsmith import Flagsmith  # imported lazily: optional at runtime

            self._client = Flagsmith(environment_key=self.s.flagsmith_server_key, api_url=self.s.flagsmith_api_url,
                                     request_timeout_seconds=3)
        flags = self._client.get_identity_flags(self.s.founder_identity, traits={"role": "founder"})
        raw = flags.get_feature_value("ops_voice")
        if isinstance(raw, str) and raw.strip():
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
        return {}

    async def get(self) -> dict:
        if not self.s.flagsmith_server_key:
            return dict(DEFAULT_VOICE)
        if self._cache and time.monotonic() - self._cache[0] < self.ttl_s:
            return dict(self._cache[1])
        try:
            data = await asyncio.wait_for(asyncio.to_thread(self._fetch_sync), timeout=5)
            voice = {**DEFAULT_VOICE, **{k: v for k, v in data.items() if v not in (None, "")}}
            self._cache = (time.monotonic(), voice)
            log("ops_voice_loaded", speaker=voice.get("speaker") or "default", pace=voice.get("pace"),
                language=voice.get("language") or "", style=voice.get("style") or "")
            return dict(voice)
        except Exception as e:  # noqa: BLE001
            log("ops_voice_fallback", error=str(e)[:200])
            return dict(DEFAULT_VOICE)
