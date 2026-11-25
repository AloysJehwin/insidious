"""Runtime agent config from Flagsmith: flag `ai_agent` (kill switch) + remote config `agent_config` (JSON).

FLAGSMITH_SERVER_KEY set -> official Flagsmith SDK with local evaluation (polls the environment document).
Otherwise (or on any error) -> env defaults (AGENT_MODEL / AGENT_PROVIDER) and flags.yaml defaults.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings
from .log import log


@dataclass
class AgentRuntime:
    ai_agent: bool = True
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    autopilot: bool = False
    max_actions: int = 4
    language_hint: str = "plain, non-technical"
    effort: str | None = None
    source: str = "env"
    extra: dict[str, Any] = field(default_factory=dict)


def _flag_default(flags_doc: dict, name: str) -> dict:
    for f in flags_doc.get("features") or []:
        if f.get("name") == name:
            return f
    return {}


def _parse_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def build_runtime(settings: Settings, ai_agent: bool | None, config_value: Any, source: str) -> AgentRuntime:
    rt = AgentRuntime(provider=settings.provider, model=settings.model, source=source)
    if ai_agent is not None:
        rt.ai_agent = bool(ai_agent)
    cfg = _parse_json(config_value)
    if cfg.get("provider") in ("anthropic", "bedrock"):
        rt.provider = cfg["provider"]
    if isinstance(cfg.get("model"), str) and cfg["model"].strip():
        rt.model = cfg["model"].strip()
    if isinstance(cfg.get("autopilot"), bool):
        rt.autopilot = cfg["autopilot"]
    try:
        if cfg.get("max_actions") is not None:
            rt.max_actions = max(1, min(8, int(cfg["max_actions"])))
    except (TypeError, ValueError):
        pass
    if isinstance(cfg.get("language_hint"), str):
        rt.language_hint = cfg["language_hint"][:200]
    if cfg.get("effort") in ("low", "medium", "high", "xhigh", "max"):
        rt.effort = cfg["effort"]
    rt.extra = {k: v for k, v in cfg.items() if k not in
                ("provider", "model", "autopilot", "max_actions", "language_hint", "effort")}
    return rt


class RuntimeConfig:
    def __init__(self, settings: Settings, client: Any = None, client_factory: Callable[..., Any] | None = None):
        self.s = settings
        self._client = client
        self._started = client is not None
        self._factory = client_factory
        self.cache_seconds = 5.0  # keeps POST /api/incident fast; the SDK itself refreshes in the background
        self._cached: AgentRuntime | None = None
        self._cached_at = 0.0

    def _offline(self) -> AgentRuntime:
        ai = _flag_default(self.s.flags_doc, "ai_agent")
        return build_runtime(self.s, bool(ai.get("default_enabled", True)) if ai else True, None, "offline")

    def _ensure_client(self) -> Any:
        if self._started:
            return self._client
        self._started = True
        if not self.s.flagsmith_server_key:
            log("flagsmith_offline", reason="FLAGSMITH_SERVER_KEY not set; using env/flags.yaml defaults")
            return None
        try:
            factory = self._factory
            if factory is None:
                from flagsmith import Flagsmith

                factory = Flagsmith
            kwargs: dict[str, Any] = dict(
                environment_key=self.s.flagsmith_server_key,
                enable_local_evaluation=True,
                environment_refresh_interval_seconds=self.s.flagsmith_refresh_seconds,
                request_timeout_seconds=3,
            )
            if self.s.flagsmith_api_url:
                kwargs["api_url"] = self.s.flagsmith_api_url
            self._client = factory(**kwargs)
            log("flagsmith_ready", local_evaluation=True)
        except Exception as e:  # noqa: BLE001
            log("flagsmith_init_failed", error=str(e)[:200])
            self._client = None
        return self._client

    def _read_sync(self) -> AgentRuntime:
        client = self._ensure_client()
        if client is None:
            return self._offline()
        try:
            flags = client.get_environment_flags()
            try:
                ai = flags.is_feature_enabled("ai_agent")
            except Exception:  # noqa: BLE001 - flag missing in Flagsmith
                ai = None
            try:
                cfg = flags.get_feature_value("agent_config")
            except Exception:  # noqa: BLE001
                cfg = None
            if ai is None:
                ai = self._offline().ai_agent
            return build_runtime(self.s, ai, cfg, "flagsmith")
        except Exception as e:  # noqa: BLE001
            log("flagsmith_eval_failed", error=str(e)[:200])
            return self._offline()

    async def get(self) -> AgentRuntime:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.cache_seconds:
            return self._cached
        try:
            rt = await asyncio.wait_for(asyncio.to_thread(self._read_sync), timeout=8)
        except Exception as e:  # noqa: BLE001
            log("flagsmith_eval_timeout", error=str(e)[:200])
            rt = self._cached or self._offline()
        self._cached, self._cached_at = rt, time.monotonic()
        return rt

    def close(self) -> None:
        try:
            if self._client is not None and hasattr(self._client, "close"):
                self._client.close()
        except Exception:  # noqa: BLE001
            pass
