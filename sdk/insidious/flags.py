"""Flag evaluation over the official Flagsmith Python SDK, with an offline mode.

* With FLAGSMITH_SERVER_KEY set: ``Flagsmith(enable_local_evaluation=True)`` polls the environment document every
  FLAGSMITH_REFRESH_SECONDS and evaluates flags in-process, so requests never call the Flagsmith API.
* Without a key (or if the client cannot start): defaults from flagsmith/flags.yaml, multivariate variants picked
  by a deterministic hash of the identity, simple segment rules evaluated locally.
* The ``heavy_features`` parent dependency is always enforced in code: if the parent is off for an identity, every
  child is off, whether or not Flagsmith flag dependencies are configured.
* Dev overrides (POST /api/dev/flags when INSIDIOUS_DEV=1) are applied on top of everything else.
* A Flagsmith error never crashes a request: we fall back to the last known environment flags, then defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger("insidious.flags")

BOOLEAN_TYPES = {"FLAG", "STANDARD", "BOOLEAN"}
# Not part of GET /api/flags: founder/agent JSON blobs, DB plumbing, and db_read_url (may hold credentials).
INTERNAL_FLAGS = frozenset({"ops_voice", "db_pool", "db_read_url", "agent_config", "read_from_replica", "ai_agent"})


@dataclass(frozen=True)
class FlagState:
    enabled: bool
    value: Any = None


def _bucket(*parts: Any) -> float:
    """Deterministic 0 <= x < 100 bucket for a set of keys."""
    digest = hashlib.md5(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return (int(digest, 16) % 10000) / 100.0


def pick_variant(flag: str, identity: str, variants: list[Mapping[str, Any]], default: Any) -> Any:
    """Deterministically map an identity into the variant percentages (remainder falls back to the default)."""
    b = _bucket(flag, identity)
    acc = 0.0
    for v in variants:
        acc += float(v.get("percentage", 0))
        if b < acc:
            return v.get("value")
    return default


def _to_number(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rule_matches(rule: Mapping[str, Any], segment: str, identity: str, traits: Mapping[str, Any]) -> bool:
    op = str(rule.get("operator", "EQUAL")).upper()
    expected = rule.get("value")
    if op == "PERCENTAGE_SPLIT":
        pct = _to_number(expected) or 0.0
        return _bucket(segment, identity) < pct
    prop = rule.get("property")
    if prop not in traits:
        return op == "IS_NOT_SET"
    actual = traits[prop]
    if isinstance(actual, Mapping):  # {"value": x, "transient": true} envelope
        actual = actual.get("value")
    if op == "IS_SET":
        return True
    a, e = _to_number(actual), _to_number(expected)
    if op in {"GREATER_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN", "LESS_THAN_INCLUSIVE"}:
        if a is None or e is None:
            return False
        return {
            "GREATER_THAN": a > e,
            "GREATER_THAN_INCLUSIVE": a >= e,
            "LESS_THAN": a < e,
            "LESS_THAN_INCLUSIVE": a <= e,
        }[op]
    if op == "EQUAL":
        return (a == e) if (a is not None and e is not None) else str(actual) == str(expected)
    if op == "NOT_EQUAL":
        return (a != e) if (a is not None and e is not None) else str(actual) != str(expected)
    if op == "CONTAINS":
        return str(expected) in str(actual)
    return False


class FlagClient:
    """Evaluates Insidious flags. Thread-safe; cheap enough to call on every request."""

    def __init__(
        self,
        flags_doc: Mapping[str, Any],
        *,
        server_key: str | None = None,
        api_url: str | None = None,
        refresh_seconds: float = 5.0,
        parent_flag: str = "heavy_features",
        children: list[str] | None = None,
        enable_events: bool = False,
        client: Any = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.features: dict[str, dict[str, Any]] = {
            f["name"]: dict(f) for f in (flags_doc.get("features") or []) if f.get("name")
        }
        self.segments: list[dict[str, Any]] = list(flags_doc.get("segments") or [])
        self.parent = parent_flag
        kids = {n for n, f in self.features.items() if f.get("depends_on") == parent_flag}
        kids.update(children or [])
        kids.discard(parent_flag)
        self.children: set[str] = kids
        self._lock = threading.Lock()
        self._dev_overrides: dict[str, Any] = {}
        self._last_env: dict[str, FlagState] | None = None
        self._last_error_log = 0.0
        self.errors = 0
        self._client = client
        self.source = "flagsmith" if client is not None else "offline"

        if self._client is None and server_key:
            try:
                factory = client_factory
                if factory is None:
                    from flagsmith import Flagsmith  # imported lazily so offline mode needs no network

                    factory = Flagsmith
                kwargs: dict[str, Any] = dict(
                    environment_key=server_key,
                    enable_local_evaluation=True,
                    environment_refresh_interval_seconds=refresh_seconds,
                    request_timeout_seconds=3,
                    default_flag_handler=self._default_flag,
                )
                if api_url:
                    kwargs["api_url"] = api_url
                if enable_events:
                    kwargs["enable_events"] = True
                self._client = factory(**kwargs)
                self.source = "flagsmith"
                log.info('{"svc":"insidious","event":"flagsmith_ready","local_evaluation":true,"refresh_s":%s}',
                         refresh_seconds)
            except Exception as exc:  # noqa: BLE001
                self._client = None
                self.source = "offline"
                log.warning('{"svc":"insidious","event":"flagsmith_init_failed","error":%s}', json.dumps(str(exc)))
        elif self._client is None:
            log.warning('{"svc":"insidious","event":"flagsmith_offline","reason":"FLAGSMITH_SERVER_KEY not set"}')

    # ------------------------------------------------------------------ helpers
    @property
    def flagsmith(self) -> Any:
        """The underlying ``flagsmith.Flagsmith`` client, or None in offline mode."""
        return self._client

    def _default_flag(self, feature_name: str) -> Any:
        from flagsmith.models import DefaultFlag

        f = self.features.get(feature_name, {})
        return DefaultFlag(enabled=bool(f.get("default_enabled", False)), value=f.get("default_value"))

    def _log_error(self, exc: Exception) -> None:
        self.errors += 1
        now = time.monotonic()
        if now - self._last_error_log > 10:  # rate-limit: a broken Flagsmith must not flood the logs
            self._last_error_log = now
            log.warning('{"svc":"insidious","event":"flagsmith_eval_failed","error":%s,"fallback":"%s"}',
                        json.dumps(str(exc)), "last_known" if self._last_env else "defaults")

    def _client_ready(self) -> bool:
        # The real SDK silently falls back to a per-request API call when the environment document is not loaded
        # yet. That would burn the free quota under load, so treat "no document" as not ready.
        return getattr(self._client, "_local_evaluation", True) is not None

    # ------------------------------------------------------------------ raw evaluation
    def _offline_states(self, identity: str | None, traits: Mapping[str, Any] | None) -> dict[str, FlagState]:
        states: dict[str, FlagState] = {}
        for name, f in self.features.items():
            enabled = bool(f.get("default_enabled", True))
            value = f.get("default_value")
            if identity and str(f.get("type", "")).upper() == "MULTIVARIATE" and f.get("variants"):
                value = pick_variant(name, identity, f["variants"], value)
            states[name] = FlagState(enabled, value)
        if identity:
            tr = dict(traits or {})
            for seg in self.segments:
                rules = seg.get("rules") or []
                if rules and all(_rule_matches(r, seg.get("name", ""), identity, tr) for r in rules):
                    for ov in seg.get("overrides") or []:
                        feat = ov.get("feature")
                        if feat in states:
                            cur = states[feat]
                            states[feat] = FlagState(bool(ov.get("enabled", cur.enabled)), ov.get("value", cur.value))
        return states

    def _flagsmith_states(self, identity: str | None, traits: Mapping[str, Any] | None) -> dict[str, FlagState]:
        if not self._client_ready():
            raise RuntimeError("environment document not loaded yet")
        if identity:
            flags = self._client.get_identity_flags(identity, traits=dict(traits or {}))
        else:
            flags = self._client.get_environment_flags()
        states: dict[str, FlagState] = {}
        for name in self.features:
            fl = flags.get_flag(name)
            states[name] = FlagState(bool(fl.enabled), fl.value)
        return states

    def raw_states(self, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> dict[str, FlagState]:
        """Flag states as Flagsmith (or offline defaults) report them, before overrides and parent enforcement."""
        if self._client is None:
            return self._offline_states(identity, traits)
        try:
            states = self._flagsmith_states(identity, traits)
            if not identity:
                self._last_env = dict(states)
            return states
        except Exception as exc:  # noqa: BLE001
            self._log_error(exc)
            if self._last_env is not None:
                base = dict(self._last_env)
                if identity:  # keep A/B buckets stable even while Flagsmith is unreachable
                    off = self._offline_states(identity, traits)
                    for n, f in self.features.items():
                        if str(f.get("type", "")).upper() == "MULTIVARIATE":
                            base[n] = FlagState(base[n].enabled, off[n].value)
                return base
            return self._offline_states(identity, traits)

    # ------------------------------------------------------------------ effective evaluation
    def evaluate(self, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> dict[str, FlagState]:
        """Effective flag states for an identity: raw states + dev overrides + parent dependency."""
        states = self.raw_states(identity, traits)
        with self._lock:
            overrides = dict(self._dev_overrides)
        for name, ov in overrides.items():
            cur = states.get(name, FlagState(False, None))
            if isinstance(ov, bool):
                states[name] = FlagState(ov, cur.value)
            elif isinstance(ov, Mapping):
                states[name] = FlagState(bool(ov.get("enabled", cur.enabled)), ov.get("value", cur.value))
            elif ov is None:
                continue
            else:  # remote-config / multivariate value, e.g. {"payment_gateway": "cashfree"}
                states[name] = FlagState(True, ov)
        parent = states.get(self.parent)
        if parent is not None and not parent.enabled:
            for child in self.children:
                if child in states:
                    states[child] = FlagState(False, states[child].value)
        return states

    def snapshot(self, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> "FlagSnapshot":
        return FlagSnapshot(self.evaluate(identity, traits))

    def is_enabled(self, flag: str, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> bool:
        st = self.evaluate(identity, traits).get(flag)
        return bool(st and st.enabled)

    def value(self, flag: str, default: Any = None, identity: str | None = None,
              traits: Mapping[str, Any] | None = None) -> Any:
        st = self.evaluate(identity, traits).get(flag)
        if st is None or st.value is None or st.value == "":
            return default
        return st.value

    def variant(self, flag: str, identity: str | None, traits: Mapping[str, Any] | None = None,
                default: Any = None) -> Any:
        if default is None:
            default = self.features.get(flag, {}).get("default_value")
        return self.value(flag, default=default, identity=identity, traits=traits)

    def env_states(self, include_internal: bool = False) -> dict[str, Any]:
        """Environment-level states for GET /api/flags (booleans for flags, values for config/multivariate).

        ``INTERNAL_FLAGS`` (ops_voice, db_*, agent_*, read_from_replica, ai_agent) are left out unless
        ``include_internal``; ``db_read_url`` is never returned in clear (it may contain a password).
        """
        states = self.evaluate(None, None)
        out: dict[str, Any] = {}
        for name, f in self.features.items():
            st = states.get(name, FlagState(False, None))
            ftype = str(f.get("type", "FLAG")).upper()
            if name in INTERNAL_FLAGS and not include_internal:
                continue
            if name == "db_read_url":
                out[name] = bool(st.value)
                continue
            out[name] = bool(st.enabled) if ftype in BOOLEAN_TYPES else st.value
        out["source"] = "flagsmith" if (self._client is not None and self._client_ready()) else "offline"
        return out

    # ------------------------------------------------------------------ dev overrides
    @property
    def dev_overrides(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._dev_overrides)

    def set_dev_overrides(self, overrides: Mapping[str, Any], replace: bool = False) -> dict[str, Any]:
        """Merge (or replace) dev overrides. A value of None removes that override."""
        with self._lock:
            if replace:
                self._dev_overrides = {}
            for k, v in overrides.items():
                if v is None:
                    self._dev_overrides.pop(k, None)
                else:
                    self._dev_overrides[k] = v
            return dict(self._dev_overrides)

    def clear_dev_overrides(self) -> None:
        with self._lock:
            self._dev_overrides = {}

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass


class FlagSnapshot:
    """Flags evaluated once for one request; read many times for free."""

    def __init__(self, states: dict[str, FlagState]):
        self.states = states

    def is_enabled(self, flag: str) -> bool:
        st = self.states.get(flag)
        return bool(st and st.enabled)

    def value(self, flag: str, default: Any = None) -> Any:
        st = self.states.get(flag)
        if st is None or st.value is None or st.value == "":
            return default
        return st.value

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.value if v.value is not None else v.enabled) for k, v in self.states.items()}
