"""Business events (``fk.track("order_placed", identity=sid, properties={...})``).

Events go to an in-memory ring buffer (for the cockpit / A/B readout) and ``order_placed`` increments
``insidious_orders_total{checkout_flow, gateway}``.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import env as _env
from .metrics import ORDERS_TOTAL

log = logging.getLogger("insidious.events")


class EventTracker:
    def __init__(self, maxlen: int = 1000, flagsmith_client: Callable[[], Any] | None = None) -> None:
        self._buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counts: collections.Counter[tuple[str, str]] = collections.Counter()  # (event, variant) -> n
        self._flagsmith_client = flagsmith_client
        self.forward_to_flagsmith = _env("INSIDIOUS_FLAGSMITH_EVENTS", "0") == "1"
        self.log_events = _env("INSIDIOUS_LOG_EVENTS", "0") == "1"

    def track(self, event: str, identity: str | None = None, properties: Mapping[str, Any] | None = None) -> dict:
        props = dict(properties or {})
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "event": event,
            "identity": identity,
            "properties": props,
        }
        with self._lock:
            self._buf.append(record)
            variant = props.get("checkout_flow")
            if variant is not None:
                self._counts[(event, str(variant))] += 1
        if event == "order_placed":
            ORDERS_TOTAL.labels(
                checkout_flow=str(props.get("checkout_flow", "unknown")), gateway=str(props.get("gateway", "unknown"))
            ).inc()
        self._experimentation_hook(record)
        return record

    # ---------------------------------------------------------------------------------------------------------
    # HOOK: Flagsmith Experimentation events.
    # Today this only logs. Set INSIDIOUS_FLAGSMITH_EVENTS=1 (and FLAGSMITH_ENABLE_EVENTS=1 so the Flagsmith client
    # is built with enable_events=True) to also forward via flagsmith.Flagsmith.track_event(...). Never raises.
    # ---------------------------------------------------------------------------------------------------------
    def _experimentation_hook(self, record: dict[str, Any]) -> None:
        level = logging.INFO if self.log_events else logging.DEBUG  # INFO under load would flood stdout
        log.log(level, '{"svc":"insidious","event":"track","name":%s,"identity":%s,"properties":%s}',
                 json.dumps(record["event"]), json.dumps(record["identity"]), json.dumps(record["properties"],
                                                                                         default=str))
        if not self.forward_to_flagsmith or self._flagsmith_client is None:
            return
        try:
            client = self._flagsmith_client()
            if client is not None and getattr(client, "_event_processor", None) is not None:
                client.track_event(record["event"], identifier=record["identity"],
                                   metadata={k: v for k, v in record["properties"].items()})
        except Exception as exc:  # noqa: BLE001
            log.warning('{"svc":"insidious","event":"flagsmith_track_failed","error":%s}', json.dumps(str(exc)))

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buf)[-n:]

    def experiment_summary(self, exposure_event: str = "checkout_started", conversion_event: str = "order_placed"
                           ) -> dict[str, dict[str, Any]]:
        """Per-variant exposure/conversion counts (variant = checkout_flow property)."""
        with self._lock:
            counts = dict(self._counts)
        variants = sorted({v for (_, v) in counts})
        out: dict[str, dict[str, Any]] = {}
        for v in variants:
            exp = counts.get((exposure_event, v), 0)
            conv = counts.get((conversion_event, v), 0)
            out[v] = {"exposures": exp, "conversions": conv, "conversion_rate": round(conv / exp, 4) if exp else None}
        return out
