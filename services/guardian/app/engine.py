"""Guardian core: sample SLOs, count breaches, fire/resolve NudgeBee events, keep state for /api/state.

Two SLO kinds: per-route latency/error SLOs (rule ``insidious-slo``) and the database pool SLO on the store's
primary (rule ``insidious-db``, route key ``db:primary``). Every fired incident also goes to the Claude ops agent
(``AGENT_URL/api/incident``, best effort). Missing data never moves the breach/recovery counters.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .log import log, utcnow_iso
from .nudgebee import EventWebhookClient, NudgeBeeGraphQL
from .prom import (DB_ROUTE_KEY, PromClient, PromError, db_in_use_query, db_size_query, db_wait_p95_query,
                   error_rate_query, p95_query, rps_query)
from .replicas import ReplicaReader

HISTORY_MAX = 180
FEED_MAX = 50
EXECUTION_TAIL_SECONDS = 120


def route_slug(route: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")
    return s or "home"


def route_title(route: str) -> str:
    first = [p for p in route.strip("/").split("/") if p and not p.startswith("{")]
    if not first:
        return "Home"
    return " ".join(w.capitalize() for w in first if w != "api") or "Home"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_readiness(reports_dir: Path) -> dict[str, Any] | None:
    try:
        files = sorted(reports_dir.glob("readiness-*.json"), key=lambda p: (p.name, p.stat().st_mtime))
    except OSError:
        return None
    for p in reversed(files):
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            continue
    return None


@dataclass
class RouteState:
    route: str
    target_p95_ms: float
    target_error_rate: float
    p95_ms: float | None = None
    error_rate: float | None = None
    breaching: bool = False
    breach_count: int = 0
    healthy_count: int = 0
    first_breach_ts: float | None = None
    incident: dict[str, Any] | None = None  # {"id","fingerprint","status","since","route",...}


@dataclass
class DbState:
    """Database pool SLO state for the store's primary (route key ``db:primary``)."""

    target_utilisation: float
    target_wait_p95_s: float
    key: str = DB_ROUTE_KEY
    in_use: float | None = None
    size: float | None = None
    utilisation: float | None = None
    wait_p95_s: float | None = None
    replica_in_use: float | None = None
    replica_size: float | None = None
    breaching: bool = False
    breach_count: int = 0
    healthy_count: int = 0
    first_breach_ts: float | None = None
    incident: dict[str, Any] | None = None


def fmt_seconds(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 1:
        return f"{v:.1f}s"
    return (f"{v:.2f}".rstrip("0").rstrip(".") or "0") + "s"


def fmt_count(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}"


class Guardian:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, *,
                 replicas: ReplicaReader | None = None, clock=time.time):
        self.s = settings
        self.client = client
        self.clock = clock
        self.prom = PromClient(settings.prometheus_url, client)
        self.webhook = EventWebhookClient(settings.nudgebee_event_webhook_url, client, settings.nudgebee_dry_run)
        self.gql = NudgeBeeGraphQL(settings.nudgebee_url, settings.nudgebee_token, settings.nudgebee_account_id,
                                   client, settings.nudgebee_dry_run)
        self.replica_reader = replicas or ReplicaReader(settings.namespace, settings.deployment, self.prom,
                                                        k8s_enabled=settings.k8s_enabled)
        self.armed = settings.armed
        self.routes: dict[str, RouteState] = {
            slo.route: RouteState(slo.route, slo.p95_ms, slo.error_rate) for slo in settings.slos
        }
        self.rps: float | None = None
        self.replicas: int | None = None
        self.flags: dict[str, Any] = {}
        self.last_execution: dict[str, Any] | None = None
        self.last_incident: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_MAX)
        self.prom_ok: bool | None = None
        self.db: DbState | None = (DbState(settings.db_target_utilisation, settings.db_target_wait_p95_s)
                                   if settings.db_slo_enabled else None)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ feed
    def add_feed(self, source: str, text: str) -> None:
        self.feed.append({"ts": utcnow_iso(), "source": source, "text": text})

    # ------------------------------------------------------------- measuring
    async def measure(self, window: str | None = None) -> dict[str, Any]:
        """One measurement of every SLO route + total RPS. Values are None when unknown."""
        w = window or self.s.rate_window
        alias = self.s.api_alias
        queries: list[tuple[str, str, str]] = []
        for r in self.routes:
            queries.append(("p95", r, p95_query(r, w, alias)))
            queries.append(("err", r, error_rate_query(r, w, alias)))
        queries.append(("rps", "", rps_query(w)))
        results = await asyncio.gather(*(self.prom.scalar(q) for _, _, q in queries), return_exceptions=True)
        out: dict[str, Any] = {"p95_ms": {}, "error_rate": {}, "rps": None, "errors": {}}
        prom_ok = True
        for (kind, route, _), res in zip(queries, results):
            if isinstance(res, BaseException):
                prom_ok = False
                out["errors"][f"{kind}:{route}"] = str(res)[:200]
                val = None
            else:
                val = res
            if kind == "p95":
                out["p95_ms"][route] = round(val * 1000, 1) if val is not None else None
            elif kind == "err":
                out["error_rate"][route] = round(val, 4) if val is not None else None
            else:
                out["rps"] = round(val, 1) if val is not None else None
        out["prom_ok"] = prom_ok
        if not prom_ok and self.prom_ok is not False:
            log("prometheus_unavailable", url=self.s.prometheus_url, errors=out["errors"])
        self.prom_ok = prom_ok
        return out

    def is_breaching(self, rs: RouteState, p95_ms: float | None, err: float | None) -> bool:
        return (p95_ms is not None and p95_ms > rs.target_p95_ms) or (
            err is not None and err > rs.target_error_rate
        )

    async def measure_db(self, window: str | None = None) -> dict[str, float | None]:
        """Primary pool in_use/size/wait p95 and replica in_use/size. Failed queries come back as None."""
        w = window or self.s.rate_window
        names = ("in_use", "size", "wait_p95_s", "replica_in_use", "replica_size")
        queries = (db_in_use_query("primary"), db_size_query("primary"), db_wait_p95_query("primary", w),
                   db_in_use_query("replica"), db_size_query("replica"))
        results = await asyncio.gather(*(self.prom.scalar(q) for q in queries), return_exceptions=True)
        out: dict[str, float | None] = {}
        for name, res in zip(names, results):
            out[name] = None if isinstance(res, BaseException) else res
        size, in_use = out["size"], out["in_use"]
        out["utilisation"] = round(in_use / size, 4) if (size and in_use is not None) else None
        if out["wait_p95_s"] is not None:
            out["wait_p95_s"] = round(out["wait_p95_s"], 4)
        return out

    def db_is_breaching(self, util: float | None, wait_s: float | None) -> bool:
        d = self.db
        if d is None:
            return False
        return (util is not None and util >= d.target_utilisation) or (
            wait_s is not None and wait_s > d.target_wait_p95_s)

    async def tick_db(self, now: float, m: dict[str, float | None]) -> None:
        d = self.db
        if d is None:
            return
        d.in_use, d.size, d.utilisation, d.wait_p95_s = m["in_use"], m["size"], m["utilisation"], m["wait_p95_s"]
        d.replica_in_use, d.replica_size = m["replica_in_use"], m["replica_size"]
        if d.utilisation is None and d.wait_p95_s is None:
            return  # unknown sample: never breach or "recover" on missing data
        d.breaching = self.db_is_breaching(d.utilisation, d.wait_p95_s)
        if d.breaching:
            d.breach_count += 1
            d.healthy_count = 0
            if d.breach_count == 1:
                d.first_breach_ts = now
        else:
            d.healthy_count += 1
            d.breach_count = 0
            d.first_breach_ts = None
        active = d.incident is not None and d.incident["status"] == "firing"
        if d.breaching and not active and d.breach_count >= self.s.breach_samples and self.armed:
            await self.fire_db(now)
        elif active and not d.breaching and d.healthy_count >= self.s.recover_samples:
            await self.resolve_db(now)

    # --------------------------------------------------------------- one tick
    async def tick(self) -> None:
        now = self.clock()
        if self.db is not None:
            m, dbm = await asyncio.gather(self.measure(), self.measure_db())
        else:
            m, dbm = await self.measure(), None
        self.rps = m["rps"]
        for route, rs in self.routes.items():
            p95, err = m["p95_ms"].get(route), m["error_rate"].get(route)
            failed = f"p95:{route}" in m["errors"] and f"err:{route}" in m["errors"]
            # No samples while traffic is ~zero = nobody is waiting = healthy. No samples while there IS traffic
            # (or Prometheus can't tell us the rate) = unknown, e.g. the pod stopped answering scrapes under load.
            idle = isinstance(self.rps, (int, float)) and self.rps < 1.0
            no_data = p95 is None and err is None and not idle
            rs.p95_ms, rs.error_rate = p95, err
            if failed or no_data:
                continue  # unknown sample: don't move the counters (never "recover" on missing data)
            rs.breaching = self.is_breaching(rs, p95, err)
            if rs.breaching:
                rs.breach_count += 1
                rs.healthy_count = 0
                if rs.breach_count == 1:
                    rs.first_breach_ts = now
            else:
                rs.healthy_count += 1
                rs.breach_count = 0
                rs.first_breach_ts = None
            active = rs.incident is not None and rs.incident["status"] == "firing"
            if rs.breaching and not active and rs.breach_count >= self.s.breach_samples and self.armed:
                await self.fire(rs, now)
            elif active and not rs.breaching and rs.healthy_count >= self.s.recover_samples:
                await self.resolve(rs, now)

        if dbm is not None:
            await self.tick_db(now, dbm)

        await asyncio.gather(self.refresh_flags(), self.refresh_replicas(), return_exceptions=True)
        await self.refresh_execution(now)
        sample: dict[str, Any] = {
            "ts": iso(now),
            "p95_ms": {r: rs.p95_ms for r, rs in self.routes.items()},
            "error_rate": {r: rs.error_rate for r, rs in self.routes.items()},
            "rps": self.rps,
            "replicas": self.replicas,
        }
        if self.db is not None:
            sample["db"] = {"utilisation": self.db.utilisation, "wait_p95_s": self.db.wait_p95_s,
                            "in_use": self.db.in_use, "size": self.db.size}
        self.history.append(sample)

    async def run_forever(self) -> None:
        log("loop_started", interval_seconds=self.s.interval_seconds, routes=list(self.routes),
            armed=self.armed, prometheus=self.s.prometheus_url)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must never die
                log("tick_error", error=f"{type(e).__name__}: {e}")
            delay = max(0.2, self.s.interval_seconds - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------- side readers
    async def refresh_flags(self) -> None:
        try:
            r = await self.client.get(f"{self.s.store_url}/api/flags")
            if r.status_code == 200 and isinstance(r.json(), dict):
                self.flags = r.json()
        except (httpx.HTTPError, ValueError):
            pass  # keep last known flags

    async def refresh_replicas(self) -> None:
        try:
            self.replicas = await self.replica_reader.read()
        except Exception:  # noqa: BLE001
            self.replicas = None

    def _all_incidents(self) -> list[dict[str, Any]]:
        incs = [rs.incident for rs in self.routes.values() if rs.incident]
        if self.db is not None and self.db.incident:
            incs.append(self.db.incident)
        return incs

    def _execution_window_open(self, now: float) -> bool:
        for inc in self._all_incidents():
            if not inc:
                continue
            if inc["status"] == "firing":
                return True
            if inc.get("resolved_ts") and now - inc["resolved_ts"] <= EXECUTION_TAIL_SECONDS:
                return True
        return False

    async def refresh_execution(self, now: float) -> None:
        wf = self.s.nudgebee_wf_incident_id
        if not wf or not self.gql.configured or not self._execution_window_open(now):
            return
        try:
            exec_id = await self.gql.latest_execution_id(wf)
            if not exec_id:
                return
            ex = await self.gql.get_execution(wf, exec_id)
            if not ex:
                return
            self.last_execution = {
                "workflow": self.s.incident_workflow_name,
                "workflow_id": wf,
                "execution_id": str(ex.get("id") or exec_id),
                "status": ex.get("status"),
                "start_time": ex.get("start_time"),
                "close_time": ex.get("close_time"),
                "error": ex.get("error"),
                "tasks": [
                    {"id": t.get("id"), "type": t.get("type"), "status": t.get("status")}
                    for t in (ex.get("tasks") or []) if isinstance(t, dict)
                ],
            }
        except Exception as e:  # noqa: BLE001
            log("execution_poll_error", error=f"{type(e).__name__}: {e}")

    # ------------------------------------------------------- fire / resolve
    def _readiness_bits(self) -> tuple[str, str]:
        rep = latest_readiness(self.s.reports_dir)
        if not rep:
            return "unknown", "From insidious guardian. No readiness report yet (run `insidious rehearse`)."
        gain = str(rep.get("capacity_gain_if_shed") or "unknown")
        b1 = (rep.get("pass1") or {}).get("breaking_rps")
        b2 = (rep.get("pass2") or {}).get("breaking_rps")
        b1s = b1 if b1 is not None else "n/a"
        b2s = b2 if b2 is not None else "no breach"
        return gain, (f"From insidious guardian. Readiness report: shedding heavy_features raised breaking point "
                      f"{b1s} → {b2s} rps ({gain}).")

    def _heavy(self) -> str:
        v = self.flags.get("heavy_features")
        return "on" if v is True else "off" if v is False else "unknown"

    def _payload(self, rs: RouteState, inc: dict[str, Any], status: str) -> dict[str, Any]:
        gain, description = self._readiness_bits()
        p95 = rs.p95_ms
        rps = self.rps
        p95_s = f"{p95:.0f}" if p95 is not None else "n/a"
        rps_s = f"{rps:.0f}" if rps is not None else "n/a"
        name = route_title(rs.route)
        target = f"{rs.target_p95_ms:.0f}"
        if status == "firing":
            if p95 is not None and p95 > rs.target_p95_ms:
                title = f"{name} p95 {p95_s}ms breaches {target}ms SLO at {rps_s} rps"
            else:
                er = (rs.error_rate or 0) * 100
                title = (f"{name} error rate {er:.1f}% breaches {rs.target_error_rate * 100:.1f}% SLO "
                         f"at {rps_s} rps")
        else:
            title = f"{name} p95 recovered to {p95_s}ms (SLO {target}ms) at {rps_s} rps"
        return {
            "id": inc["id"],
            "fingerprint": inc["fingerprint"],
            "status": status,
            "title": title,
            "rule_name": self.s.rule_name,
            "severity": "high",
            "subject": {"name": self.s.deployment, "type": "Deployment", "namespace": self.s.namespace},
            "labels": {
                "route": rs.route,
                "p95_ms": p95_s,
                "error_rate": f"{(rs.error_rate or 0):.4f}",
                "rps": rps_s,
                "heavy_features": self._heavy(),
                "capacity_gain_if_shed": gain,
                "replicas": str(self.replicas) if self.replicas is not None else "unknown",
            },
            "description": description,
        }

    async def fire(self, rs: RouteState, now: float) -> None:
        first = rs.first_breach_ts or now
        slug = route_slug(rs.route)
        inc = {
            "id": f"slo-{slug}-p95-{int(first)}",
            "fingerprint": f"{self.s.app_name}/{self.s.deployment}/{slug}-p95",
            "status": "firing",
            "since": iso(first),
            "route": rs.route,
            "kind": "latency",
            "rule_name": self.s.rule_name,
        }
        payload = self._payload(rs, inc, "firing")
        inc["title"] = payload["title"]
        inc["labels"] = payload["labels"]
        res = await self.webhook.send(payload)
        inc["sent"] = res["sent"]
        inc["dry_run"] = res["dry_run"]
        rs.incident = inc
        self.last_incident = inc
        self.last_execution = None  # a fresh incident gets a fresh execution
        tag = " (dry-run)" if res["dry_run"] else "" if res["sent"] else f" (send failed: {res['error']})"
        self.add_feed("guardian", f"SLO breach: {payload['title']}. Event {inc['id']} sent to NudgeBee{tag}")
        log("incident_fired", id=inc["id"], route=rs.route, p95_ms=rs.p95_ms, rps=self.rps, **res)
        await self.notify_agent(inc)

    async def resolve(self, rs: RouteState, now: float) -> None:
        inc = rs.incident
        if not inc:
            return
        payload = self._payload(rs, inc, "resolved")
        res = await self.webhook.send(payload)
        inc["status"] = "resolved"
        inc["resolved_at"] = iso(now)
        inc["resolved_ts"] = now
        self.last_incident = inc
        tag = " (dry-run)" if res["dry_run"] else "" if res["sent"] else f" (send failed: {res['error']})"
        self.add_feed("guardian", f"Recovered: {payload['title']}. Event {inc['id']} resolved{tag}")
        log("incident_resolved", id=inc["id"], route=rs.route, p95_ms=rs.p95_ms, **res)

    # ------------------------------------------------------- database SLO
    def _db_title(self, status: str) -> str:
        d = self.db
        assert d is not None
        busy = f"{fmt_count(d.in_use)}/{fmt_count(d.size)}"
        if status == "firing":
            if d.utilisation is not None and d.wait_p95_s is not None:
                return f"Database connections {busy} busy, shoppers waiting {fmt_seconds(d.wait_p95_s)}"
            if d.utilisation is not None:
                return f"Database connections {busy} busy"
            return f"Shoppers waiting {fmt_seconds(d.wait_p95_s)} for a database connection"
        return f"Database recovered: connections {busy} busy, wait {fmt_seconds(d.wait_p95_s)}"

    def _read_from_replica(self) -> str:
        d = self.db
        if d is None or d.replica_size is None:
            return "off"
        return "on" if d.replica_size > 0 else "off"

    def _db_payload(self, inc: dict[str, Any], status: str) -> dict[str, Any]:
        d = self.db
        assert d is not None
        rps_s = f"{self.rps:.0f}" if self.rps is not None else "n/a"
        util_s = f"{d.utilisation:.2f}" if d.utilisation is not None else "n/a"
        wait_s = f"{d.wait_p95_s:.3f}" if d.wait_p95_s is not None else "n/a"
        return {
            "id": inc["id"],
            "fingerprint": inc["fingerprint"],
            "status": status,
            "title": self._db_title(status),
            "rule_name": self.s.db_rule_name,
            "severity": "high",
            "subject": {"name": self.s.deployment, "type": "Deployment", "namespace": self.s.namespace},
            "labels": {
                "route": DB_ROUTE_KEY,
                "db": "primary",
                "pool_in_use": fmt_count(d.in_use),
                "pool_size": fmt_count(d.size),
                "pool_utilisation": util_s,
                "wait_p95_s": wait_s,
                "target_utilisation": f"{d.target_utilisation:.2f}",
                "target_wait_p95_s": f"{d.target_wait_p95_s:.3f}",
                "rps": rps_s,
                "replicas": str(self.replicas) if self.replicas is not None else "unknown",
                "read_from_replica": self._read_from_replica(),
                "heavy_features": self._heavy(),
            },
            "description": (
                f"From insidious guardian. Store database pool on the primary: {fmt_count(d.in_use)} of "
                f"{fmt_count(d.size)} connections in use (utilisation {util_s}, SLO {d.target_utilisation:.2f}); "
                f"connection wait p95 {wait_s}s (SLO {d.target_wait_p95_s:.3f}s) at {rps_s} rps. Levers: resize the "
                f"pool (Flagsmith db_pool), route reads to a replica (read_from_replica + db_read_url), scale the DB "
                f"instance class, or shed heavy_features."
            ),
        }

    async def fire_db(self, now: float) -> None:
        d = self.db
        assert d is not None
        first = d.first_breach_ts or now
        inc = {
            "id": f"slo-db-primary-{int(first)}",
            "fingerprint": f"{self.s.app_name}/{self.s.deployment}/db-primary",
            "status": "firing",
            "since": iso(first),
            "route": DB_ROUTE_KEY,
            "kind": "db",
            "rule_name": self.s.db_rule_name,
        }
        payload = self._db_payload(inc, "firing")
        inc["title"] = payload["title"]
        inc["labels"] = payload["labels"]
        res = await self.webhook.send(payload)
        inc["sent"] = res["sent"]
        inc["dry_run"] = res["dry_run"]
        d.incident = inc
        self.last_incident = inc
        self.last_execution = None
        tag = " (dry-run)" if res["dry_run"] else "" if res["sent"] else f" (send failed: {res['error']})"
        self.add_feed("guardian", f"DB pool saturated: {payload['title']}. Event {inc['id']} sent to NudgeBee{tag}")
        log("incident_fired", id=inc["id"], route=DB_ROUTE_KEY, utilisation=d.utilisation,
            wait_p95_s=d.wait_p95_s, rps=self.rps, **res)
        await self.notify_agent(inc)

    async def resolve_db(self, now: float) -> None:
        d = self.db
        if d is None or not d.incident:
            return
        inc = d.incident
        payload = self._db_payload(inc, "resolved")
        res = await self.webhook.send(payload)
        inc["status"] = "resolved"
        inc["resolved_at"] = iso(now)
        inc["resolved_ts"] = now
        self.last_incident = inc
        tag = " (dry-run)" if res["dry_run"] else "" if res["sent"] else f" (send failed: {res['error']})"
        self.add_feed("guardian", f"Recovered: {payload['title']}. Event {inc['id']} resolved{tag}")
        log("incident_resolved", id=inc["id"], route=DB_ROUTE_KEY, utilisation=d.utilisation,
            wait_p95_s=d.wait_p95_s, **res)

    # ---------------------------------------------------------- AI agent
    async def notify_agent(self, inc: dict[str, Any]) -> dict[str, Any] | None:
        """POST a fired incident to AGENT_URL/api/incident (2 s timeout). Never raises; skipped without AGENT_URL."""
        url = self.s.agent_url
        if not url:
            return None
        body = {
            "source": "guardian",
            "incident": {k: v for k, v in inc.items() if k != "resolved_ts"},
            "state": self.snapshot(),
        }
        tok = self.s.internal_token  # both headers while receivers migrate from X-FestiveKit-Token
        headers = {"X-Insidious-Token": tok, "X-FestiveKit-Token": tok} if tok else {}
        try:
            r = await self.client.post(f"{url}/api/incident", json=body, headers=headers,
                                       timeout=self.s.agent_timeout)
        except httpx.HTTPError as e:
            log("agent_incident_error", id=inc.get("id"), error=f"{type(e).__name__}: {e}"[:300])
            return {"ok": False, "error": type(e).__name__}
        ok = 200 <= r.status_code < 300
        plan_id = None
        if ok:
            try:
                data = r.json()
                plan_id = data.get("plan_id") if isinstance(data, dict) else None
            except ValueError:
                plan_id = None
            if plan_id:
                inc["agent_plan_id"] = plan_id
            self.add_feed("guardian", "Incident handed to the AI ops agent" + (f" (plan {plan_id})" if plan_id else ""))
            log("agent_incident_sent", id=inc.get("id"), http_status=r.status_code, plan_id=plan_id)
        else:
            log("agent_incident_failed", id=inc.get("id"), http_status=r.status_code, body=r.text[:300])
        return {"ok": ok, "status": r.status_code, "plan_id": plan_id}

    # ---------------------------------------------------------- rehearsal
    async def rehearse_sample(self, label: str, target_rps: float | None, window: str | None = None) -> dict:
        m = await self.measure(window)
        breaching = {
            r: self.is_breaching(rs, m["p95_ms"].get(r), m["error_rate"].get(r)) for r, rs in self.routes.items()
        }
        log("rehearse_sample", label=label, target_rps=target_rps, p95_ms=m["p95_ms"], rps=m["rps"])
        return {
            "label": label,
            "target_rps": target_rps,
            "ts": utcnow_iso(),
            "p95_ms": m["p95_ms"],
            "error_rate": m["error_rate"],
            "rps": m["rps"],
            "breaching": breaching,
            "prom_ok": m["prom_ok"],
        }

    # ------------------------------------------------------------ snapshot
    def _incident_view(self) -> dict[str, Any] | None:
        firing = [inc for inc in self._all_incidents() if inc["status"] == "firing"]
        inc = max(firing, key=lambda i: i["since"]) if firing else self.last_incident
        if not inc:
            return None
        return {k: v for k, v in inc.items() if k != "resolved_ts"}

    def _db_view(self) -> dict[str, Any] | None:
        d = self.db
        if d is None:
            return None
        replica = None
        if d.replica_size:  # the SDK sets the replica pool size to 0 when reads are not routed to a replica
            replica = {"in_use": d.replica_in_use, "size": d.replica_size}
        return {
            "primary": {
                "route": d.key,
                "in_use": d.in_use,
                "size": d.size,
                "utilisation": d.utilisation,
                "wait_p95_s": d.wait_p95_s,
                "target_utilisation": d.target_utilisation,
                "target_wait_p95_s": d.target_wait_p95_s,
                "breaching": d.breaching,
                "breach_count": d.breach_count,
            },
            "replica": replica,
            "read_from_replica": replica is not None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "slo": {
                r: {
                    "p95_ms": rs.p95_ms,
                    "error_rate": rs.error_rate,
                    "target_p95_ms": rs.target_p95_ms,
                    "target_error_rate": rs.target_error_rate,
                    "breaching": rs.breaching,
                    "breach_count": rs.breach_count,
                }
                for r, rs in self.routes.items()
            },
            "rps": self.rps,
            "replicas": {self.s.deployment: self.replicas},
            "db": self._db_view(),
            "flags": self.flags,
            "incident": self._incident_view(),
            "last_execution": self.last_execution,
            "history": list(self.history),
            "feed": list(self.feed),
            "prometheus_ok": self.prom_ok,
        }
