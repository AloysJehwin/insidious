"""Agent orchestration: incident -> telemetry -> plan -> approval -> guarded execution."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable

import httpx

from .aws import RdsFactory, default_rds_factory
from .catalog import GuardrailError, validate_actions
from .config import Settings
from .executors import DRY_SUFFIX, Executor
from .flags import AgentRuntime, RuntimeConfig
from .flagsmith_admin import FlagsmithAdmin
from .k8s import K8sClient
from .log import log, utcnow_iso
from .models import AskRequest, DecisionRequest, IncidentRequest, Option, Plan
from .nudgebee import NudgeBeeClient
from .outbox import Outbox
from .planner import Planner
from .store import PlanStore, new_plan_id
from .telemetry import Telemetry, extract_facts


class HTTPProblem(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def founder_text(status: str, results: list[dict], reason: str = "") -> str:
    if status == "rejected":
        return "Okay, I will not change anything for now."
    if status == "guardrail":
        return ("I did not change anything: the plan went past one of the safety limits, so I stopped. "
                "I will suggest something safer if the problem continues.")
    practice = any(r.get("dry_run") for r in results)
    done = [_lower_first(r["detail"].replace(DRY_SUFFIX, "")) for r in results if r.get("ok")]
    failed = [_lower_first(r["detail"]) for r in results if not r.get("ok")]
    tail = " This was a practice run, so nothing was really changed." if practice else ""
    if not failed:
        return (("Done: " + "; ".join(done) + ".") if done else "Done.") + tail
    if not done:
        return "I could not make the change: " + "; ".join(failed) + ". Nothing else was changed."
    return "I did part of it. Done: " + "; ".join(done) + ". Not done: " + "; ".join(failed) + "." + tail


class AgentService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, *, k8s: K8sClient | None = None,
                 rds_factory: RdsFactory | None = None, runtime: RuntimeConfig | None = None,
                 claude_client: Any = None, sleep=asyncio.sleep):
        self.s = settings
        self.client = client
        self.store = PlanStore(settings.data_dir)
        self.k8s = k8s or K8sClient(enabled=settings.k8s_enabled)
        self.nb = NudgeBeeClient(settings, client)
        if rds_factory is None and settings.rds_id:
            rds_factory = default_rds_factory(settings.aws_region)
        self.rds_factory = rds_factory
        self.outbox = Outbox(settings, client)
        self.telemetry = Telemetry(settings, client, self.k8s, self.nb, rds_factory)
        self.runtime = runtime or RuntimeConfig(settings)
        self.planner = Planner(settings, claude_client=claude_client)
        self.flagsmith = FlagsmithAdmin(settings.flagsmith_admin_url, settings.flagsmith_org_api_key,
                                        settings.flagsmith_environment_key, client)
        self.executor = Executor(settings, self.k8s, self.flagsmith, self.nb, self.outbox, rds_factory, sleep=sleep)
        self.tasks: set[asyncio.Task] = set()
        self.sleep = sleep
        self._lock = asyncio.Lock()

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ------------------------------------------------------------ incidents
    async def submit_incident(self, req: IncidentRequest) -> dict:
        incident = dict(req.incident or (req.state or {}).get("incident") or {})
        iid = str(incident.get("id") or f"manual-{utcnow_iso()}")
        incident["id"] = iid
        async with self._lock:
            active = self.store.active_for(iid) or self.store.any_active()
            if str(incident.get("status", "firing")).lower() == "resolved":
                log("incident_resolved_ignored", incident_id=iid)
                return {"plan_id": active.id if active else None, "status": "ignored", "reason": "incident resolved"}
            if active:
                log("incident_deduplicated", incident_id=iid, plan_id=active.id)
                return {"plan_id": active.id, "deduplicated": True}
            rt = await self.runtime.get()
            if not rt.ai_agent:
                log("ai_agent_off_notify_only", incident_id=iid)
                self.spawn(self._notify_only(incident, req.state))
                return {"plan_id": None, "mode": "notify_only"}
            plan = Plan(id=new_plan_id(), created_at=utcnow_iso(), incident_id=iid, status="planning",
                        founder_summary_en="Looking into it.", technical_summary="collecting telemetry",
                        options=[Option(key="reject", title_en="Do nothing for now")])
            self.store.save(plan)
        log("incident_accepted", incident_id=iid, plan_id=plan.id, source=req.source)
        self.spawn(self._plan(plan.id, incident, req.state, rt))
        return {"plan_id": plan.id}

    async def _notify_only(self, incident: dict, state: dict) -> None:
        tel = await self.telemetry.collect(incident, state, include_rca=False, include_logs=False)
        facts = extract_facts(tel, incident, self.s)
        from .planner import rule_answer

        text = ("Heads-up: " + rule_answer("", facts, None) +
                " The AI helper is switched off, so I am not suggesting changes.")
        await self.outbox.feed("Agent: AI planner is OFF (ai_agent flag); founder notified only")
        await self.outbox.notify_quiet(text, "warning", True)

    async def _plan(self, plan_id: str, incident: dict, state: dict, rt: AgentRuntime) -> None:
        try:
            tel = await self.telemetry.collect(incident, state)
            facts = extract_facts(tel, incident, self.s)
            draft, meta = await self.planner.make_plan(incident, tel, facts, rt)
            rec = draft.options[0] if draft.options and draft.options[0].key != "reject" else None
            replicas_to = next((a.params.get("replicas") for a in (rec.actions if rec else [])
                                if a.type == "k8s.scale"), None)
            numbers = {k: v for k, v in {
                "p95_ms": facts.get("p95_ms"), "rps": facts.get("rps"), "replicas_from": facts.get("replicas"),
                "replicas_to": replicas_to, "cpu_pct_of_limit": facts.get("cpu_pct_of_limit"),
                "db_utilisation": facts.get("db_utilisation"), "db_wait_p95_s": facts.get("db_wait_p95_s"),
                "gain": facts.get("capacity_gain_if_shed"), "cost": rec.cost_en if rec else None,
            }.items() if v is not None}
            plan = self.store.get(plan_id)
            plan = self.store.save(plan.model_copy(update={
                **draft.model_dump(), "options": draft.options, "status": "pending_approval",
                "planner": meta.get("planner", "rules"), "model": meta.get("model"),
                "incident_kind": facts.get("kind", ""), "numbers": numbers,
                "error": meta.get("fallback_reason"),
            }))
            log("plan_ready", plan_id=plan_id, planner=plan.planner, kind=plan.incident_kind,
                options=[o.key for o in plan.options])
            await self.outbox.feed(f"Agent: plan ready ({plan.planner}): "
                                   + (rec.title_en if rec else "no action needed"))
            if rt.autopilot and rec:
                res = await self.decide(plan_id, DecisionRequest(option="recommended", channel="autopilot",
                                                                 actor="agent"))
                await self.outbox.notify_quiet("Autopilot: " + res.get("founder_text_en", ""), "info", True)
                return
            payload = {
                "plan_id": plan.id,
                "action": "agent_plan",
                "summary_en": plan.technical_summary,
                "founder_summary": plan.founder_summary_en,
                "options": [{"key": o.key, "title_en": o.title_en} for o in plan.options],
                "numbers": numbers,
            }
            approval: dict = {}
            for attempt in range(3):  # ops-bot may be restarting; the plan stays readable via GET either way
                approval = await self.outbox.approval_request(payload)
                if approval.get("ok"):
                    break
                await self.sleep(3 * (attempt + 1))
            plan = self.store.get(plan_id)
            self.store.save(plan.model_copy(update={"approval": approval}))
        except Exception as e:  # noqa: BLE001 - mark the plan failed, never crash the loop
            log("plan_failed", plan_id=plan_id, error=f"{type(e).__name__}: {str(e)[:300]}")
            plan = self.store.get(plan_id)
            if plan:
                self.store.save(plan.model_copy(update={"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}))
            await self.outbox.feed(f"Agent: planning failed ({type(e).__name__})")

    # ------------------------------------------------------------ decisions
    async def decide(self, plan_id: str, req: DecisionRequest) -> dict:
        plan = self.store.get(plan_id)
        if plan is None:
            raise HTTPProblem(404, f"plan {plan_id} not found")
        if plan.status != "pending_approval":
            raise HTTPProblem(409, f"plan {plan_id} is {plan.status}, not pending_approval")
        opt = plan.option(req.option)
        if opt is None:
            raise HTTPProblem(400, f"plan {plan_id} has no option {req.option!r}")
        decision = {**req.model_dump(), "ts": utcnow_iso()}
        who = f"{req.actor} via {req.channel}"

        if req.option == "reject" or not opt.actions:
            text = founder_text("rejected", [])
            self.store.save(plan.model_copy(update={"status": "rejected", "decision": decision, "results": []}))
            log("plan_rejected", plan_id=plan_id, by=who)
            await self.outbox.feed(f"Agent: {who} chose to do nothing for now (plan {plan_id})")
            return {"status": "rejected", "results": [], "founder_text_en": text}

        try:  # guardrails first: nothing runs if any action is outside them
            actions = validate_actions(opt.actions, self.s.guardrails)
        except GuardrailError as e:
            results = [{"type": "guardrail", "ok": False, "dry_run": False, "ts": utcnow_iso(),
                        "detail": "Stopped: the plan went past a safety limit", "technical": str(e)}]
            self.store.save(plan.model_copy(update={"status": "failed", "decision": decision, "results": results,
                                                    "error": f"guardrail: {e}"}))
            log("plan_guardrail_rejected", plan_id=plan_id, reason=str(e))
            await self.outbox.feed(f"Agent: plan {plan_id} option {req.option} blocked by guardrails: {e}")
            return {"status": "failed", "results": results, "founder_text_en": founder_text("guardrail", results)}

        # status flip happens before any await, so a concurrent second decision gets 409
        self.store.save(plan.model_copy(update={"status": "executing", "decision": decision}))
        await self.outbox.feed(f"Agent: {who} approved '{opt.title_en}' - executing {len(actions)} step(s)")
        results = await self.executor.run(actions, context=plan_id)
        status = "done" if all(r["ok"] for r in results) else "failed"
        text = founder_text(status, results)
        plan = self.store.get(plan_id)
        self.store.save(plan.model_copy(update={"status": status, "results": results}))
        log("plan_executed", plan_id=plan_id, option=req.option, status=status)
        return {"status": status, "results": results, "founder_text_en": text}

    # ------------------------------------------------------------ Q&A
    async def ask(self, req: AskRequest) -> dict:
        rt = await self.runtime.get()
        tel = await self.telemetry.collect({}, None, include_rca=False, include_logs=False)
        facts = extract_facts(tel, {}, self.s)
        latest = self.store.latest()
        answer, source = await self.planner.answer(req.question, tel, facts,
                                                   latest.model_dump() if latest else None, rt)
        return {"answer_en": answer, "source": source}

    async def aclose(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        for t in list(self.tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.executor.aclose()
        await self.k8s.aclose()
        self.runtime.close()
