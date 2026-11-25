"""Telemetry collectors. Each one is defensive and time-bounded: a failure becomes {"error": ...}, never a crash."""
from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from typing import Any, Awaitable

import httpx

from .aws import RdsFactory, describe_instance
from .catalog import parse_cpu, parse_memory
from .config import Settings
from .k8s import K8sClient
from .log import log, utcnow_iso
from .nudgebee import NudgeBeeClient

LOG_PATTERN = re.compile(r"ERROR|Traceback|Exception")
MAX_LOG_LINES = 50


async def _safe(name: str, coro: Awaitable[Any], timeout: float) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        log("telemetry_timeout", collector=name, timeout_s=timeout)
        return {"error": f"timed out after {timeout:g}s"}
    except Exception as e:  # noqa: BLE001
        log("telemetry_failed", collector=name, error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _cpu_usage_cores(v: Any) -> float | None:
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([num]?)", s)
    if not m:
        return None
    n = float(m.group(1))
    return {"n": n / 1e9, "u": n / 1e6, "m": n / 1e3, "": n}[m.group(2)]


def _mem_mib(v: Any) -> float | None:
    try:
        return round(parse_memory(v) / 2**20, 1)
    except ValueError:
        return None


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


class Telemetry:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, k8s: K8sClient, nudgebee: NudgeBeeClient,
                 rds_factory: RdsFactory | None = None):
        self.s = settings
        self.client = client
        self.k8s = k8s
        self.nb = nudgebee
        self.rds_factory = rds_factory

    async def collect(self, incident: dict | None = None, state: dict | None = None, *, include_rca: bool = True,
                      include_logs: bool = True) -> dict:
        names = ["guardian", "kubernetes", "db_pool", "rds", "readiness"]
        coros = [
            _safe("guardian", self.guardian_state(state), 5),
            _safe("kubernetes", self.kubernetes(include_logs), 12),
            _safe("db_pool", self.db_pool(), 6),
            _safe("rds", self.rds(), 10),
            _safe("readiness", self.readiness(), 3),
        ]
        if include_rca:
            names.append("nudgebee_rca")
            coros.append(_safe("nudgebee_rca", self.nudgebee_rca(incident or {}), self.s.rca_timeout_s + 10))
        results = await asyncio.gather(*coros)
        out = dict(zip(names, results))
        out["collected_at"] = utcnow_iso()
        return out

    # ------------------------------------------------------------ guardian
    async def guardian_state(self, given: dict | None) -> dict:
        state: dict = {}
        try:
            r = await self.client.get(f"{self.s.guardian_url}/api/state", timeout=4)
            if r.status_code == 200:
                state = r.json() or {}
        except httpx.HTTPError as e:
            log("guardian_state_failed", error=type(e).__name__)
        if not state:
            state = dict(given or {})
            if not state:
                return {"error": "guardian unreachable and no state in the request"}
            state["_source"] = "incident_request"
        out = {k: v for k, v in state.items() if k not in ("history", "feed")}
        out["history"] = list(state.get("history") or [])[-12:]
        out["feed"] = list(state.get("feed") or [])[-8:]
        return out

    # ------------------------------------------------------------ kubernetes
    async def kubernetes(self, include_logs: bool = True) -> dict:
        if not self.k8s.available:
            return {"available": False, "reason": "not running in-cluster (no service account)"}
        ns, app = self.s.namespace, self.s.deployment
        deps, pods, metrics = await asyncio.gather(
            self.k8s.list_deployments(ns), self.k8s.list_pods(ns), self.k8s.pod_metrics(ns), return_exceptions=True)
        out: dict[str, Any] = {"available": True, "namespace": ns}

        limits: dict[str, float] = {}  # container name -> cpu limit cores (for the app deployment)
        if isinstance(deps, Exception):
            out["deployments_error"] = str(deps)[:200]
        else:
            out["deployments"] = []
            for d in deps:
                spec = d.get("spec") or {}
                st = d.get("status") or {}
                containers = []
                for c in ((spec.get("template") or {}).get("spec") or {}).get("containers") or []:
                    res = c.get("resources") or {}
                    containers.append({"name": c.get("name"), "resources": res})
                    if d.get("metadata", {}).get("name") == app:
                        try:
                            limits[c.get("name")] = parse_cpu((res.get("limits") or {}).get("cpu"))
                        except ValueError:
                            pass
                out["deployments"].append({
                    "name": (d.get("metadata") or {}).get("name"),
                    "replicas": spec.get("replicas"),
                    "ready": st.get("readyReplicas") or 0,
                    "available": st.get("availableReplicas") or 0,
                    "containers": containers,
                })

        app_pods: list[dict] = []
        if isinstance(pods, Exception):
            out["pods_error"] = str(pods)[:200]
        else:
            out["pods"] = []
            for p in pods:
                md, st = p.get("metadata") or {}, p.get("status") or {}
                cs = st.get("containerStatuses") or []
                reason = None
                for c in cs:
                    term = ((c.get("lastState") or {}).get("terminated") or {})
                    if term.get("reason"):
                        reason = term["reason"]
                        break
                row = {
                    "name": md.get("name"),
                    "app": (md.get("labels") or {}).get("app"),
                    "phase": st.get("phase"),
                    "ready": bool(cs) and all(c.get("ready") for c in cs),
                    "restarts": sum(int(c.get("restartCount") or 0) for c in cs),
                    "last_termination_reason": reason,
                }
                out["pods"].append(row)
                if row["app"] == app:
                    app_pods.append({**row, "containers": [c.get("name") for c in
                                                            (p.get("spec") or {}).get("containers") or []]})

        if isinstance(metrics, Exception):
            out["metrics_error"] = str(metrics)[:200]
        else:
            usage = {}
            for m in metrics:
                name = (m.get("metadata") or {}).get("name")
                cpu, mem, pct = 0.0, 0.0, []
                for c in m.get("containers") or []:
                    u = c.get("usage") or {}
                    cores = _cpu_usage_cores(u.get("cpu")) or 0.0
                    cpu += cores
                    mem += _mem_mib(u.get("memory")) or 0.0
                    if c.get("name") in limits and limits[c["name"]] > 0:
                        pct.append(cores / limits[c["name"]])
                row = {"cpu_m": round(cpu * 1000), "memory_mi": round(mem, 1)}
                if pct:
                    row["cpu_pct_of_limit"] = round(100 * max(pct), 1)
                usage[name] = row
            out["pod_metrics"] = usage

        if include_logs and app_pods:
            out["error_logs"] = await self._error_logs(ns, app_pods[:3])
        return out

    async def _error_logs(self, ns: str, pods: list[dict]) -> list[str] | dict:
        async def one(p: dict) -> list[str]:
            container = self.s.deployment if self.s.deployment in (p.get("containers") or []) else None
            text = await self.k8s.pod_log(ns, p["name"], container=container, tail_lines=200)
            return [f"{p['name']}: {ln[:300]}" for ln in text.splitlines() if LOG_PATTERN.search(ln)]

        res = await asyncio.gather(*[one(p) for p in pods], return_exceptions=True)
        lines: list[str] = []
        errors = []
        for r in res:
            if isinstance(r, Exception):
                errors.append(str(r)[:150])
            else:
                lines.extend(r)
        if errors and not lines:
            return {"error": "; ".join(errors)}
        return lines[-MAX_LOG_LINES:]

    # ------------------------------------------------------------ prometheus
    async def _vector(self, query: str) -> list[tuple[dict, float | None]]:
        r = await self.client.get(f"{self.s.prometheus_url}/api/v1/query", params={"query": query}, timeout=4)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(str(body.get("error"))[:200])
        out = []
        for row in (body.get("data") or {}).get("result") or []:
            try:
                out.append((row.get("metric") or {}, _f(row["value"][1])))
            except (KeyError, IndexError, TypeError):
                continue
        return out

    async def db_pool(self) -> dict:
        queries = {
            "size_total": "sum(insidious_db_pool_size) by (db)",
            "pool_size_per_pod": "max(insidious_db_pool_size) by (db)",
            "in_use": "sum(insidious_db_pool_in_use) by (db)",
            "wait_p95_s": "histogram_quantile(0.95, sum(rate(insidious_db_pool_wait_seconds_bucket[1m])) by (le, db))",
            "query_p95_s": "histogram_quantile(0.95, sum(rate(insidious_db_query_seconds_bucket[1m])) by (le, db))",
        }
        res = await asyncio.gather(*[self._vector(q) for q in queries.values()], return_exceptions=True)
        dbs: dict[str, dict] = {}
        errors = []
        for key, r in zip(queries, res):
            if isinstance(r, Exception):
                errors.append(f"{key}: {type(r).__name__}")
                continue
            for labels, v in r:
                dbs.setdefault(labels.get("db") or "primary", {})[key] = None if v is None else round(v, 4)
        for d in dbs.values():
            if d.get("size_total"):
                d["utilisation"] = round((d.get("in_use") or 0) / d["size_total"], 3)
        if errors and not dbs:
            return {"error": "; ".join(errors)}
        return dbs

    # ------------------------------------------------------------ rds
    async def rds(self) -> dict:
        if not self.s.rds_id:
            return {"configured": False}
        if self.rds_factory is None:
            return {"configured": True, "error": "no AWS client"}
        info = await asyncio.to_thread(describe_instance, self.rds_factory(), self.s.rds_id)
        return {"configured": True, **info}

    # ------------------------------------------------------------ readiness report
    async def readiness(self) -> dict:
        files = sorted(Path(self.s.reports_dir).glob("readiness-*.json"))
        if not files:
            return {"available": False}
        data = json.loads(files[-1].read_text())
        return {
            "file": files[-1].name,
            "generated_at": data.get("generated_at"),
            "slo": data.get("slo"),
            "breaking_rps_heavy_on": (data.get("pass1") or {}).get("breaking_rps"),
            "breaking_rps_heavy_off": (data.get("pass2") or {}).get("breaking_rps"),
            "capacity_gain_if_shed": data.get("capacity_gain_if_shed"),
            "recommendation": data.get("recommendation"),
        }

    # ------------------------------------------------------------ nudgebee
    async def nudgebee_rca(self, incident: dict) -> dict:
        if not self.s.nudgebee_rca:
            return {"skipped": "AGENT_NUDGEBEE_RCA=0"}
        if self.nb.dry:
            return {"skipped": "NudgeBee dry-run or not configured"}
        title = incident.get("title") or f"Incident {incident.get('id', '')} on {self.s.deployment}"
        q = (f"{title}. Namespace {self.s.namespace}, deployment {self.s.deployment}. "
             "What is the root cause? Answer in 3 short sentences.")
        return await self.nb.investigate(q, timeout_s=self.s.rca_timeout_s)


# ---------------------------------------------------------------- facts for planners
def _app_deployment(tel: dict, name: str) -> dict:
    for d in ((tel.get("kubernetes") or {}).get("deployments") or []):
        if d.get("name") == name:
            return d
    return {}


def extract_facts(tel: dict, incident: dict, settings: Settings) -> dict:
    g = tel.get("guardian") or {}
    labels = incident.get("labels") or {}
    facts: dict[str, Any] = {}

    # latency
    route, p95 = None, _f(labels.get("p95_ms"))
    worst = None
    for r, v in (g.get("slo") or {}).items():
        if isinstance(v, dict) and not str(r).startswith("db:") and v.get("p95_ms") is not None:
            if worst is None or (v.get("p95_ms") or 0) > (worst[1].get("p95_ms") or 0):
                worst = (r, v)
    if worst:
        route = worst[0]
        p95 = p95 if p95 is not None else _f(worst[1].get("p95_ms"))
        facts["target_p95_ms"] = worst[1].get("target_p95_ms")
        facts["latency_breaching"] = bool(worst[1].get("breaching"))
    facts["route"] = labels.get("route") or route
    facts["p95_ms"] = p95
    facts["rps"] = _f(labels.get("rps")) if labels.get("rps") is not None else _f(g.get("rps"))

    # replicas / resources
    dep = _app_deployment(tel, settings.deployment)
    replicas = dep.get("replicas")
    if replicas is None:
        replicas = (g.get("replicas") or {}).get(settings.deployment)
    facts["replicas"] = int(replicas) if isinstance(replicas, (int, float)) else None
    for c in dep.get("containers") or []:
        if c.get("name") == settings.deployment or len(dep.get("containers") or []) == 1:
            lim = (c.get("resources") or {}).get("limits") or {}
            facts["container"] = c.get("name")
            facts["cpu_limit"] = lim.get("cpu")
            facts["memory_limit"] = lim.get("memory")
            break
    pm = (tel.get("kubernetes") or {}).get("pod_metrics") or {}
    pods = [p for p in ((tel.get("kubernetes") or {}).get("pods") or []) if p.get("app") == settings.deployment]
    pcts = [pm.get(p["name"], {}).get("cpu_pct_of_limit") for p in pods]
    pcts = [x for x in pcts if x is not None]
    facts["cpu_pct_of_limit"] = round(sum(pcts) / len(pcts), 1) if pcts else None
    facts["oom_killed"] = any(p.get("last_termination_reason") == "OOMKilled" for p in pods)
    facts["restarts"] = sum(int(p.get("restarts") or 0) for p in pods)

    # database
    pool = (tel.get("db_pool") or {}).get("primary") or {}
    facts["db_utilisation"] = pool.get("utilisation")
    facts["db_wait_p95_s"] = pool.get("wait_p95_s")
    facts["db_pool_size"] = int(pool["pool_size_per_pod"]) if pool.get("pool_size_per_pod") else None
    rds = tel.get("rds") or {}
    facts["rds_class"] = rds.get("class")
    facts["rds_status"] = rds.get("status")
    facts["rds_replicas"] = rds.get("replicas") or []
    flags = g.get("flags") or {}
    facts["heavy_features"] = flags.get("heavy_features")
    facts["read_from_replica"] = flags.get("read_from_replica")
    facts["capacity_gain_if_shed"] = labels.get("capacity_gain_if_shed") or (tel.get("readiness") or {}).get(
        "capacity_gain_if_shed")
    facts["kind"] = classify(incident, g, facts)
    return facts


def classify(incident: dict, guardian: dict, facts: dict) -> str:
    """'db' for a database-pool incident, else 'cpu' (CPU / latency)."""
    rule = str(incident.get("rule_name") or "").lower()
    iid = str(incident.get("id") or "").lower()
    labels = incident.get("labels") or {}
    route = str(incident.get("route") or labels.get("route") or "")
    if rule.endswith("-db") or "insidious-db" in rule or route.startswith("db:") or iid.startswith("db-") \
            or "-db-" in iid or str(incident.get("kind") or "").lower() == "db":
        return "db"
    slo = guardian.get("slo") or {}
    db_breach = any(str(k).startswith("db:") and isinstance(v, dict) and v.get("breaching") for k, v in slo.items())
    route_breach = any(not str(k).startswith("db:") and isinstance(v, dict) and v.get("breaching")
                       for k, v in slo.items())
    if db_breach and not route_breach:
        return "db"
    util, wait = facts.get("db_utilisation"), facts.get("db_wait_p95_s")
    cpu = facts.get("cpu_pct_of_limit")
    db_hot = (util is not None and util >= 0.9) or (wait is not None and wait > 0.25)
    if db_hot and not route_breach and (cpu is None or cpu < 80):
        return "db"
    return "cpu"


def prompt_json(tel: dict, limit: int = 24000) -> str:
    s = json.dumps(tel, default=str, separators=(",", ":"), sort_keys=True)
    if len(s) <= limit:
        return s
    slim = dict(tel)
    k = dict(slim.get("kubernetes") or {})
    if isinstance(k.get("error_logs"), list):
        k["error_logs"] = k["error_logs"][-15:]
    slim["kubernetes"] = k
    g = dict(slim.get("guardian") or {})
    g["history"] = (g.get("history") or [])[-4:]
    g.pop("feed", None)
    slim["guardian"] = g
    s = json.dumps(slim, default=str, separators=(",", ":"), sort_keys=True)
    return s[:limit]
