"""The Insidious facade.

    from insidious import Insidious
    fk = Insidious.from_env()
    app.add_middleware(fk.metrics_middleware)
    fk.mount_metrics(app)

    @fk.degradable("recommendations", fallback=cached_top_sellers)
    def recommendations(user): ...

    gateway = fk.config("payment_gateway", default="razorpay")
    variant = fk.variant("checkout_flow", identity=sid, traits={"cart_value": 2400})
    fk.track("order_placed", identity=sid, properties={"checkout_flow": variant, "gateway": gateway})

    db = fk.database()          # needs the [db] extra; primary from DATABASE_URL, pool from flag db_pool
    with db.read().connect() as conn: ...   # replica when read_from_replica is ON and db_read_url is set
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Iterable, Mapping

from . import config as _config
from .degrade import degradable as _degradable
from .degrade import get_context, set_context
from .events import EventTracker
from .flags import FlagClient, FlagSnapshot
from .metrics import MetricsMiddleware, metrics_endpoint_factory, set_flag_gauges

DEFAULT_METRICS_EXCLUDE = ("/metrics", "/healthz")


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


class Insidious:
    def __init__(
        self,
        flags: FlagClient,
        settings: Mapping[str, Any] | None = None,
        *,
        dev_mode: bool = False,
        metrics_exclude: Iterable[str] = DEFAULT_METRICS_EXCLUDE,
        route_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.flags = flags
        self.route_aliases: dict[str, str] = dict(route_aliases or {})
        self.settings: dict[str, Any] = dict(settings or {})
        self.dev_mode = dev_mode
        self.metrics_exclude = set(metrics_exclude)
        self.events = EventTracker(flagsmith_client=lambda: self.flags.flagsmith)
        self._database: Any = None
        self._database_lock = threading.Lock()

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, *, client: Any = None, metrics_exclude: Iterable[str] = DEFAULT_METRICS_EXCLUDE,
                 route_aliases: Mapping[str, str] | None = None, **overrides: Any) -> "Insidious":
        """Build from environment variables, insidious.yaml and flagsmith/flags.yaml.

        FLAGSMITH_SERVER_KEY (unset = offline mode), FLAGSMITH_API_URL, FLAGSMITH_REFRESH_SECONDS (default 5),
        FLAGSMITH_ENABLE_EVENTS, INSIDIOUS_DEV, INSIDIOUS_CONFIG, INSIDIOUS_FLAGS.
        """
        settings = _config.load_config(overrides.pop("config_path", None))
        flags_doc = _config.load_flags(overrides.pop("flags_path", None))
        degr = settings.get("degradable") or {}
        parent = degr.get("parent_flag", "heavy_features")
        children = [f.get("flag") for f in degr.get("features", []) if f.get("flag")]
        try:
            refresh = float(os.environ.get("FLAGSMITH_REFRESH_SECONDS", "5") or 5)
        except ValueError:
            refresh = 5.0
        server_key = overrides.pop("server_key", os.environ.get("FLAGSMITH_SERVER_KEY", "")).strip()
        if server_key.startswith("ser.xxxx"):  # the .env.example placeholder: stay offline
            server_key = ""
        flag_client = FlagClient(
            flags_doc,
            server_key=server_key or None,
            api_url=os.environ.get("FLAGSMITH_API_URL") or None,
            refresh_seconds=refresh,
            parent_flag=parent,
            children=children,
            enable_events=_truthy(os.environ.get("FLAGSMITH_ENABLE_EVENTS")),
            client=client,
        )
        dev_mode = overrides.pop("dev_mode", _truthy(_config.env("INSIDIOUS_DEV")))
        return cls(flag_client, settings, dev_mode=dev_mode, metrics_exclude=metrics_exclude,
                   route_aliases=route_aliases)

    # ------------------------------------------------------------------ flags
    def identify(self, identity: str | None, traits: Mapping[str, Any] | None = None) -> None:
        """Set the identity/traits used by @degradable functions for the rest of this request/thread context."""
        set_context(identity, traits)

    @property
    def current_identity(self) -> tuple[str | None, Mapping[str, Any] | None]:
        return get_context()

    def is_enabled(self, flag: str, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> bool:
        return self.flags.is_enabled(flag, identity, traits)

    def value(self, flag: str, default: Any = None, identity: str | None = None,
              traits: Mapping[str, Any] | None = None) -> Any:
        return self.flags.value(flag, default, identity, traits)

    config = value  # fk.config("payment_gateway", default="razorpay")

    def variant(self, flag: str, identity: str | None, traits: Mapping[str, Any] | None = None,
                default: Any = None) -> Any:
        return self.flags.variant(flag, identity, traits, default)

    def snapshot(self, identity: str | None = None, traits: Mapping[str, Any] | None = None) -> FlagSnapshot:
        return self.flags.snapshot(identity, traits)

    def env_states(self) -> dict[str, Any]:
        """Environment-level flags for GET /api/flags; also refreshes insidious_flag_state for every boolean flag."""
        set_flag_gauges(self.flags.env_states(include_internal=True))
        return self.flags.env_states()

    def set_dev_overrides(self, overrides: Mapping[str, Any], replace: bool = False) -> dict[str, Any]:
        if not self.dev_mode:
            raise PermissionError("dev overrides need INSIDIOUS_DEV=1")
        out = self.flags.set_dev_overrides(overrides, replace=replace)
        self.env_states()  # refresh gauges immediately
        return out

    # ------------------------------------------------------------------ degradation
    def degradable(self, flag: str, fallback: Any = None, *, fallback_on_error: bool = True) -> Callable:
        return _degradable(flag, fallback, get_flags=lambda: self.flags, fallback_on_error=fallback_on_error)

    # ------------------------------------------------------------------ database
    def database(self, primary_url: str | None = None, **kwargs: Any) -> Any:
        """The app's :class:`insidious.db.FlagRoutedDatabase` (created on first call, then reused).

        Primary = ``primary_url`` or env DATABASE_URL (SQLite file fallback); pool size from remote config
        ``db_pool``; reads go to ``db_read_url`` while ``read_from_replica`` is ON. Needs ``insidious[db]``.
        """
        with self._database_lock:
            if self._database is None:
                from .db import FlagRoutedDatabase

                self._database = FlagRoutedDatabase(self.flags, primary_url, **kwargs)
            return self._database

    # ------------------------------------------------------------------ events
    def track(self, event: str, identity: str | None = None, properties: Mapping[str, Any] | None = None) -> dict:
        return self.events.track(event, identity, properties)

    # ------------------------------------------------------------------ metrics
    @property
    def metrics_middleware(self) -> type:
        """A middleware class for ``app.add_middleware(fk.metrics_middleware)``."""
        exclude = self.metrics_exclude
        aliases = self.route_aliases

        class _InsidiousMetrics(MetricsMiddleware):
            def __init__(self, app: Callable) -> None:
                super().__init__(app, exclude_routes=exclude, route_aliases=aliases)

        return _InsidiousMetrics

    def metrics_endpoint(self) -> Callable:
        return metrics_endpoint_factory(before_render=self.env_states)

    def mount_metrics(self, app: Any, path: str = "/metrics") -> None:
        app.add_route(path, self.metrics_endpoint(), methods=["GET"], include_in_schema=False)

    def install(self, app: Any) -> None:
        """Add the metrics middleware and the /metrics route in one call."""
        app.add_middleware(self.metrics_middleware)
        self.mount_metrics(app)

    def close(self) -> None:
        if self._database is not None:
            self._database.dispose()
        self.flags.close()
