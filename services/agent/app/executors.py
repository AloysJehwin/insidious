"""Executors for the action catalog. Every executor dry-runs when its service is unconfigured or AGENT_DRY_RUN=1.

Each result: {"type", "params", "ok", "dry_run", "detail" (plain words for the founder), "technical", "ts"}.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from .aws import RdsFactory, describe_instance
from .config import Settings
from .flagsmith_admin import FlagsmithAdmin
from .k8s import K8sClient
from .log import log, utcnow_iso
from .models import Action
from .nudgebee import NudgeBeeClient
from .outbox import Outbox

FLAG_WORDS = {
    "heavy_features": "product suggestions, HD photos and live reviews",
    "recommendations": "product suggestions",
    "hd_images": "HD photos",
    "live_reviews": "live reviews",
    "festive_collection": "the festive collection",
}
SECRET_FLAGS = {"db_read_url"}
DRY_SUFFIX = " (practice run, nothing was changed)"


def flag_detail(flag: str, enabled: bool) -> str:
    if flag in FLAG_WORDS:
        return f"{'Turned' if enabled else 'Paused'} {FLAG_WORDS[flag]}" + (" back on" if enabled else "")
    if flag == "read_from_replica":
        return "Browsing now reads from the database copy" if enabled else "Browsing reads from the main database again"
    return f"Turned {flag.replace('_', ' ')} {'on' if enabled else 'off'}"


def intent(a: Action) -> str:
    p = a.params
    return {
        "flagsmith.set_flag": lambda: f"turn {FLAG_WORDS.get(p.get('flag'), str(p.get('flag')).replace('_', ' '))} "
                                      f"{'on' if p.get('enabled') else 'off'}",
        "flagsmith.set_value": lambda: f"change the {str(p.get('flag')).replace('_', ' ')} setting",
        "k8s.scale": lambda: f"change the store to {p.get('replicas')} servers",
        "k8s.set_resources": lambda: "give the store servers more power",
        "db.set_pool": lambda: "open more database lanes",
        "aws.rds.create_read_replica": lambda: "start a database copy",
        "aws.rds.modify_instance_class": lambda: "move the database to a bigger machine",
        "nudgebee.run_workflow": lambda: "start the NudgeBee check",
        "notify.founder": lambda: "send you the message",
    }.get(a.type, lambda: a.type)()


def build_read_url(database_url: str, endpoint: str, port: int | None = None) -> str | None:
    """postgresql://<user>:<password>@<endpoint>:5432/<dbname> from DATABASE_URL credentials (kept URL-encoded)."""
    if not database_url or not endpoint:
        return None
    u = urlsplit(database_url)
    if not u.scheme.startswith("postgres"):
        return None
    userinfo = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    scheme = u.scheme if u.scheme.startswith("postgresql") else "postgresql"
    return urlunsplit((scheme, f"{userinfo}{endpoint}:{port or 5432}", u.path or "/postgres", u.query, ""))


class Executor:
    def __init__(self, settings: Settings, k8s: K8sClient, flagsmith: FlagsmithAdmin, nudgebee: NudgeBeeClient,
                 outbox: Outbox, rds_factory: RdsFactory | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.s = settings
        self.k8s = k8s
        self.fs = flagsmith
        self.nb = nudgebee
        self.out = outbox
        self.rds_factory = rds_factory
        self.sleep = sleep
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ dry-run switches
    @property
    def fs_dry(self) -> bool:
        return self.s.dry_run or not self.fs.configured

    @property
    def k8s_dry(self) -> bool:
        return self.s.dry_run or not self.k8s.available

    @property
    def rds_dry(self) -> bool:
        return self.s.dry_run or not self.s.rds_id or self.rds_factory is None

    @property
    def notify_dry(self) -> bool:
        return self.s.dry_run or not self.s.ops_bot_url

    # ------------------------------------------------------------ run
    async def run(self, actions: list[Action], context: str = "") -> list[dict]:
        results = []
        for a in actions:
            ts = utcnow_iso()
            try:
                detail, technical, dry = await self._run_one(a)
                res = {"type": a.type, "params": self._safe_params(a), "ok": True, "dry_run": dry,
                       "detail": detail + (DRY_SUFFIX if dry else ""), "technical": technical, "ts": ts}
            except Exception as e:  # noqa: BLE001 - keep going; report the failure in plain words
                technical = f"{type(e).__name__}: {str(e)[:300]}"
                res = {"type": a.type, "params": self._safe_params(a), "ok": False, "dry_run": False,
                       "detail": f"Could not {intent(a)}", "technical": technical, "ts": ts}
            log("action_executed", action=a.type, ok=res["ok"], dry_run=res["dry_run"], technical=res["technical"],
                context=context)
            await self.out.feed(f"Agent: {res['detail']}" + ("" if res["ok"] else f" ({res['technical'][:120]})"))
            results.append(res)
        return results

    @staticmethod
    def _safe_params(a: Action) -> dict:
        p = dict(a.params)
        if a.type == "flagsmith.set_value" and p.get("flag") in SECRET_FLAGS:
            p["value"] = "<redacted>"
        return p

    async def _run_one(self, a: Action) -> tuple[str, str, bool]:
        p = a.params
        if a.type == "flagsmith.set_flag":
            detail = flag_detail(p["flag"], p["enabled"])
            tech = f"flagsmith {p['flag']} enabled={p['enabled']}"
            if self.fs_dry:
                return detail, "dry-run: " + tech, True
            await self.fs.set_flag(p["flag"], p["enabled"])
            return detail, tech, False

        if a.type == "flagsmith.set_value":
            shown = "<redacted>" if p["flag"] in SECRET_FLAGS else str(p["value"])[:80]
            detail = f"Changed the {p['flag'].replace('_', ' ')} setting"
            if p["flag"] == "payment_gateway":
                detail = f"Switched payments to {shown}"
            tech = f"flagsmith {p['flag']} value={shown}"
            if self.fs_dry:
                return detail, "dry-run: " + tech, True
            await self.fs.set_value(p["flag"], p["value"])
            return detail, tech, False

        if a.type == "db.set_pool":
            value = json.dumps({"pool_size": p["pool_size"], "max_overflow": p["max_overflow"],
                                "timeout_s": p["timeout_s"]}, separators=(",", ":"))
            detail = f"Opened more database lanes ({p['pool_size']} per server)"
            tech = f"flagsmith db_pool={value}"
            if self.fs_dry:
                return detail, "dry-run: " + tech, True
            await self.fs.set_value("db_pool", value)
            return detail, tech, False

        if a.type == "k8s.scale":
            n = p["replicas"]
            detail = f"The store now runs on {n} server{'s' if n != 1 else ''}"
            tech = f"scale {p['namespace']}/{p['deployment']} replicas={n}"
            if self.k8s_dry:
                return detail, "dry-run: " + tech, True
            await self.k8s.scale(p["namespace"], p["deployment"], n)
            return detail, tech, False

        if a.type == "k8s.set_resources":
            limits = {"cpu": p["cpu_limit"], "memory": p["memory_limit"]}
            requests = {k: v for k, v in (("cpu", p.get("cpu_request")), ("memory", p.get("memory_request"))) if v}
            detail = "Gave each store server more computing power and memory; they restart one by one"
            tech = f"resources {p['namespace']}/{p['deployment']}/{p['container']} limits={limits} requests={requests}"
            if self.k8s_dry:
                return detail, "dry-run: " + tech, True
            await self.k8s.set_resources(p["namespace"], p["deployment"], p["container"], limits, requests or None)
            return detail, tech, False

        if a.type == "aws.rds.create_read_replica":
            detail = "Started making a read-only copy of the database; it will be ready in about 15 minutes"
            tech = f"rds create_db_instance_read_replica {p['replica_id']} <- {p['source_db_id']} ({p['instance_class']})"
            if self.rds_dry:
                return detail, "dry-run: " + tech, True
            rds = self.rds_factory()
            try:
                await asyncio.to_thread(rds.create_db_instance_read_replica, DBInstanceIdentifier=p["replica_id"],
                                        SourceDBInstanceIdentifier=p["source_db_id"],
                                        DBInstanceClass=p["instance_class"])
            except Exception as e:  # noqa: BLE001
                code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
                if code != "DBInstanceAlreadyExists":
                    raise
                tech += " (already exists; watching it)"
            self._spawn(self._watch_rds(p["replica_id"], None, self._replica_ready))
            return detail, tech, False

        if a.type == "aws.rds.modify_instance_class":
            detail = "Started moving the database to a bigger machine; about 10 minutes, with a short pause"
            tech = f"rds modify_db_instance {p['db_id']} -> {p['instance_class']} ApplyImmediately"
            if self.rds_dry:
                return detail, "dry-run: " + tech, True
            rds = self.rds_factory()
            await asyncio.to_thread(rds.modify_db_instance, DBInstanceIdentifier=p["db_id"],
                                    DBInstanceClass=p["instance_class"], ApplyImmediately=True)
            self._spawn(self._watch_rds(p["db_id"], p["instance_class"], self._resize_ready))
            return detail, tech, False

        if a.type == "nudgebee.run_workflow":
            detail = "Started the NudgeBee check"
            tech = f"nudgebee workflow_execute {p['workflow_id']}"
            if self.nb.dry:
                return detail, "dry-run: " + tech, True
            res = await self.nb.workflow_execute(p["workflow_id"], p.get("inputs") or {})
            return detail, f"{tech} execution_id={res.get('execution_id')}", False

        if a.type == "notify.founder":
            detail = "Sent you a message"
            tech = f"ops-bot /nb/notify: {p['text_en'][:80]}"
            if self.notify_dry:
                return detail, "dry-run: " + tech, True
            await self.out.notify(p["text_en"], "info", True)
            return detail, tech, False

        raise ValueError(f"no executor for {a.type}")

    # ------------------------------------------------------------ RDS background watchers
    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _watch_rds(self, db_id: str, want_class: str | None,
                         on_ready: Callable[[dict], Awaitable[None]]) -> None:
        deadline = time.monotonic() + self.s.rds_watch_seconds
        last = None
        while time.monotonic() < deadline:
            await self.sleep(self.s.rds_poll_seconds)
            try:
                info = await asyncio.to_thread(describe_instance, self.rds_factory(), db_id)
            except Exception as e:  # noqa: BLE001
                log("rds_watch_poll_failed", db_id=db_id, error=f"{type(e).__name__}: {str(e)[:150]}")
                continue
            if info.get("status") != last:
                last = info.get("status")
                log("rds_watch_status", db_id=db_id, status=last, instance_class=info.get("class"))
            if info.get("status") == "available" and (want_class is None or info.get("class") == want_class):
                try:
                    await on_ready(info)
                except Exception as e:  # noqa: BLE001
                    log("rds_on_ready_failed", db_id=db_id, error=f"{type(e).__name__}: {str(e)[:200]}")
                    await self.out.feed(f"Agent: database {db_id} is ready but the follow-up failed ({type(e).__name__})")
                return
        log("rds_watch_timeout", db_id=db_id, last_status=last)
        await self.out.feed(f"Agent: still waiting for database {db_id} (status {last}); stopped watching")
        await self.out.notify_quiet("The database change is taking longer than expected. I will keep the store "
                                    "running as it is.", "warning", True)

    async def _replica_ready(self, info: dict) -> None:
        endpoint = info.get("endpoint")
        log("rds_replica_available", db_id=info.get("id"), endpoint=endpoint)
        url = build_read_url(self.s.database_url, endpoint, info.get("port"))
        if not url:
            await self.out.feed(f"Agent: database copy {info.get('id')} is ready at {endpoint}; DATABASE_URL is not "
                                "set, so db_read_url must be set by hand")
            await self.out.notify_quiet("The database copy is ready. An engineer needs to connect it before shoppers "
                                        "can use it.", "warning", True)
            return
        if self.fs_dry:
            await self.out.feed("Agent: database copy ready; would set db_read_url and turn read_from_replica ON"
                                + DRY_SUFFIX)
        else:
            await self.fs.set_value("db_read_url", url)
            await self.fs.set_flag("read_from_replica", True)
            await self.out.feed(f"Agent: database copy {info.get('id')} is ready; read_from_replica turned ON")
        await self.out.notify_quiet("Database copy is ready, shoppers now read from it. Pages should feel faster.",
                                    "info", True)

    async def _resize_ready(self, info: dict) -> None:
        await self.out.feed(f"Agent: database {info.get('id')} now runs on {info.get('class')}")
        await self.out.notify_quiet("The database has moved to the bigger machine and is running normally.", "info",
                                    True)

    async def aclose(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        for t in list(self.tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
