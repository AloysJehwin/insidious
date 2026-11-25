"""Planners: Claude (structured output via the `submit_plan` tool) with a deterministic rule-based fallback."""
from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import ValidationError

from .catalog import (
    COST_INR_PER_HOUR,
    CPU_CORE_INR_PER_HOUR,
    GuardrailError,
    describe_catalog,
    format_cpu,
    format_memory,
    parse_cpu,
    parse_memory,
    rds_prefixes,
    validate_action,
    validate_actions,
)
from .config import Settings
from .flags import AgentRuntime
from .log import log
from .models import Action, Option, PlanDraft
from .telemetry import prompt_json

PLAN_TOOL = "submit_plan"
# Models that accept the server-side refusal fallback (`fallbacks`, beta server-side-fallback-2026-07-01).
SERVER_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class PlanInvalid(Exception):
    """Model output that is missing, refused, or fails validation."""


class PlannerUnavailable(Exception):
    """The Claude provider cannot be used at all (no credentials, auth failure, unknown model)."""


# ======================================================================= rule-based planner
def _money(x: float) -> str:
    return f"₹{x:g}" if x == int(x) else f"₹{x:.1f}"


def _cost(x: float) -> str:
    return "No extra cost" if x <= 0 else f"About {_money(round(x, 1))} more per hour"


def _secs(ms: Any) -> str:
    try:
        return f"{float(ms) / 1000:.1f} seconds"
    except (TypeError, ValueError):
        return ""


def _keep_valid(opt: Option, g: dict) -> Option | None:
    try:
        return opt.model_copy(update={"actions": validate_actions(opt.actions, g)})
    except GuardrailError as e:
        log("rule_option_dropped", option=opt.key, reason=str(e))
        return None


def rule_plan(facts: dict, s: Settings) -> PlanDraft:
    g = s.guardrails
    reject = Option(key="reject", title_en="Do nothing for now", impact_en="Things stay as they are.", cost_en="")
    if facts.get("kind") == "db":
        options, summary, tech = _rule_db(facts, s)
    else:
        options, summary, tech = _rule_cpu(facts, s)
    kept = [o for o in (_keep_valid(o, g) for o in options) if o is not None]
    if kept:
        kept[0] = kept[0].model_copy(update={"key": "recommended"})
        for i, o in enumerate(kept[1:2], start=1):
            kept[i] = o.model_copy(update={"key": f"alt{i}"})
    return PlanDraft(founder_summary_en=summary, technical_summary=tech, options=kept[:2] + [reject])


def _rule_cpu(f: dict, s: Settings) -> tuple[list[Option], str, str]:
    g = s.guardrails
    mx = int(g.get("max_replicas", 5))
    cur = int(f.get("replicas") or 1)
    target = min(3, mx)
    if cur >= target:
        target = min(cur + 1, mx)
    extra = max(0, target - cur)
    rec_actions = [Action(type="flagsmith.set_flag", params={"flag": "heavy_features", "enabled": False})]
    if extra:
        rec_actions.append(Action(type="k8s.scale", params={"namespace": s.namespace, "deployment": s.deployment,
                                                            "replicas": target}))
    title = f"Pause extras + add {extra} server{'s' if extra != 1 else ''}" if extra else "Pause extra features"
    rec = Option(key="recommended", title_en=title,
                 impact_en="Checkout should be fast again in about a minute. Product suggestions, HD photos and live "
                           "reviews pause until the rush is over.",
                 cost_en=_cost(extra * COST_INR_PER_HOUR["store_server"]), actions=rec_actions)

    options = [rec]
    try:
        cur_cpu = parse_cpu(f.get("cpu_limit") or "500m")
        cur_mem = parse_memory(f.get("memory_limit") or "512Mi")
    except ValueError:
        cur_cpu, cur_mem = 0.5, 512 * 2**20
    max_cpu, max_mem = parse_cpu(g.get("max_cpu_limit", "2")), parse_memory(g.get("max_memory_limit", "2Gi"))
    new_cpu = min(max(cur_cpu * 2, cur_cpu + 0.5), max_cpu)
    new_mem = min(max(cur_mem * 2, 512 * 2**20), max_mem)
    if new_cpu > cur_cpu or new_mem > cur_mem:
        extra_cores = max(0.0, new_cpu - cur_cpu) * max(cur, 1)
        options.append(Option(
            key="alt1", title_en="Give each server more power",
            impact_en="Each server can handle more shoppers. Servers restart one by one, about 2 minutes.",
            cost_en=_cost(extra_cores * CPU_CORE_INR_PER_HOUR),
            actions=[Action(type="k8s.set_resources", params={
                "namespace": s.namespace, "deployment": s.deployment, "container": f.get("container") or s.deployment,
                "cpu_limit": format_cpu(new_cpu), "memory_limit": format_memory(int(new_mem))})]))

    slow = _secs(f.get("p95_ms"))
    goal = _secs(f.get("target_p95_ms"))
    what = f"checkout is slow ({slow}" + (f", goal under {goal})" if goal else ")") if slow else "checkout is slow"
    plan_words = "pausing product suggestions" + (f" and adding {extra} more server{'s' if extra != 1 else ''}"
                                                   if extra else "")
    summary = (f"Lots of shoppers right now and {what}. I suggest {plan_words}. "
               f"{rec.cost_en if extra else 'This costs nothing extra'}.")
    db = (f"db pool util {f.get('db_utilisation')}, wait p95 {f.get('db_wait_p95_s')}s; "
          if f.get("db_utilisation") is not None else "")
    tech = (f"{s.deployment}: p95 {f.get('p95_ms')}ms on {f.get('route')} at {f.get('rps')} rps; replicas {cur}; "
            f"CPU {f.get('cpu_pct_of_limit')}% of {f.get('cpu_limit')} limit; heavy_features={f.get('heavy_features')}; "
            f"{db}restarts {f.get('restarts')}, OOMKilled={f.get('oom_killed')}. "
            f"Plan: shed heavy_features + scale to {target}.")
    return options, summary, tech


_CLASS_ORDER = ["db.t4g.micro", "db.t4g.small", "db.t4g.medium", "db.t4g.large"]


def _class_price(cls: str | None) -> float:
    return COST_INR_PER_HOUR.get(str(cls), 0.0)


def _rule_db(f: dict, s: Settings) -> tuple[list[Option], str, str]:
    g = s.guardrails
    max_pool = int(g.get("max_db_pool_size", 40))
    pool = int(f.get("db_pool_size") or 5)
    new_pool = min(max(pool * 2, pool + 5), max_pool)
    actions = [Action(type="db.set_pool", params={"pool_size": new_pool, "max_overflow": min(5, max_pool),
                                                  "timeout_s": 5})]
    cost = 0.0
    replica_started = False
    if s.rds_id and not f.get("rds_replicas"):
        replica = Action(type="aws.rds.create_read_replica", params={
            "source_db_id": s.rds_id, "replica_id": f"{s.rds_id}-read-1", "instance_class": "db.t4g.micro"})
        try:
            actions.append(validate_action(replica, g))
            cost += COST_INR_PER_HOUR["db.t4g.micro"]
            replica_started = True
        except GuardrailError as e:
            log("rule_replica_skipped", reason=str(e))
    if not replica_started:
        actions.append(Action(type="flagsmith.set_flag", params={"flag": "read_from_replica", "enabled": True}))
    rec = Option(
        key="recommended",
        title_en="More database lanes + a read copy" if replica_started else "More database lanes + read copy",
        impact_en=("Pages get faster in about a minute; the read-only copy of the database is ready in about 15 "
                   "minutes and takes over browsing." if replica_started else
                   "Pages get faster in about a minute; browsing reads from the database copy."),
        cost_en=_cost(cost), actions=actions)
    options = [rec]

    allowed = [str(x) for x in g.get("allowed_db_instance_classes") or []]
    cur_cls = f.get("rds_class") or "db.t4g.micro"
    order = [c for c in _CLASS_ORDER if c in allowed] or allowed
    nxt = None
    if cur_cls in order and order.index(cur_cls) + 1 < len(order):
        nxt = order[order.index(cur_cls) + 1]
    elif cur_cls not in order and order:
        nxt = order[0]
    if nxt:
        db_id = s.rds_id or f"{(rds_prefixes(g) or [''])[0]}db"
        options.append(Option(
            key="alt1", title_en="Move to a bigger database",
            impact_en="The database gets about twice the power. Short pause of 1-2 minutes while it switches.",
            cost_en=_cost(max(0.0, _class_price(nxt) - _class_price(cur_cls))),
            actions=[Action(type="aws.rds.modify_instance_class", params={"db_id": db_id, "instance_class": nxt})]))

    copy_words = "adding a read-only copy of the database for browsing" if replica_started else \
        "sending browsing to the database copy"
    summary = (f"Many shoppers are waiting in line for the database, so pages are slow. I suggest letting the store "
               f"open more database connections and {copy_words}. {rec.cost_en if cost else 'This costs nothing extra'}.")
    tech = (f"DB pool primary: size/pod {pool}, utilisation {f.get('db_utilisation')}, wait p95 "
            f"{f.get('db_wait_p95_s')}s; RDS {s.rds_id or 'n/a'} class {f.get('rds_class')} status "
            f"{f.get('rds_status')} replicas {f.get('rds_replicas')}; read_from_replica={f.get('read_from_replica')}; "
            f"p95 {f.get('p95_ms')}ms at {f.get('rps')} rps. Plan: pool {pool}->{new_pool} + "
            f"{'create read replica' if replica_started else 'read_from_replica ON'}.")
    return options, summary, tech


def rule_answer(question: str, facts: dict, latest_plan: dict | None) -> str:
    parts = []
    slow, goal = _secs(facts.get("p95_ms")), _secs(facts.get("target_p95_ms"))
    if facts.get("latency_breaching"):
        parts.append(f"Checkout is slow right now: about {slow}" + (f" (goal: under {goal})." if goal else "."))
    elif slow:
        parts.append(f"The store looks healthy: checkout takes about {slow}.")
    else:
        parts.append("I could not read the store's speed right now.")
    if facts.get("replicas"):
        n = facts["replicas"]
        parts.append(f"{n} store server{'s are' if n != 1 else ' is'} running.")
    if facts.get("db_utilisation") is not None and facts["db_utilisation"] >= 0.9:
        parts.append("The database is very busy and shoppers are waiting for it.")
    if latest_plan and latest_plan.get("status") == "pending_approval":
        rec = next((o for o in latest_plan.get("options") or [] if o.get("key") == "recommended"), None)
        if rec:
            parts.append(f"I have suggested: {rec.get('title_en')} ({rec.get('cost_en')}). Waiting for your OK.")
    return " ".join(parts)


# ======================================================================= Claude planner
SYSTEM_PROMPT = """You are the SRE planner for Insidious, which keeps a small Indian D2C online store ("Jeffy") running during festive sales. The store is run by a NON-technical founder who approves every change from WhatsApp.

Your job: read the incident and the telemetry, work out what is going wrong, and propose a short plan.

Rules:
- Choose actions ONLY from the action catalog below, with exactly the listed params. Respect every guardrail; an action outside the guardrails is rejected and wastes the founder's time.
- Return at most 3 options. The first option has key "recommended" (your best fix). You may add one alternative with key "alt1". The LAST option is always {"key": "reject", "title_en": "Do nothing for now", "impact_en": "...", "cost_en": "", "actions": []}.
- Each non-reject option has 1 to {max_actions} actions, executed in order.
- founder_summary_en: plain words a shop owner understands, at most 300 characters. Say what is happening and what you recommend, with its cost. No jargon: never say pods, p95, latency, replicas, CPU, Kubernetes, k8s, RDS, connection pool, flag, or SLO. Say "servers", "slow", "database copy", "pause product suggestions" instead. Do not end with a question; the messaging layer adds "Shall I go ahead?".
- title_en: at most 40 characters, plain words (it becomes a WhatsApp button).
- impact_en: one plain sentence on what the founder will notice and how soon.
- cost_en: estimated extra cost in rupees per hour using the cost constants below (e.g. "About ₹8 more per hour"), or "No extra cost".
- technical_summary: concise engineer-facing evidence (numbers from the telemetry) and the reasoning.
- Prefer cheap, fast, reversible fixes first (pausing heavy features costs nothing and works in seconds), then adding servers, then database changes.
- For a slow checkout with high server load: pause heavy_features and/or add servers. For a busy database (connection pool near full, waiting for connections): enlarge the pool with db.set_pool, and add or use a read copy for browsing.
- Never touch namespaces, databases or flags that are not mentioned in the catalog and guardrails.

Respond ONLY by calling the submit_plan tool exactly once.

{catalog}"""


def _variant(t: str, props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {
        "type": {"type": "string", "enum": [t]},
        "params": {"type": "object", "properties": props, "required": required, "additionalProperties": False}},
        "required": ["type", "params"], "additionalProperties": False}


def plan_tool(g: dict, strict: bool = True) -> dict:
    S = {"type": "string"}
    ns = {"type": "string", "enum": [str(x) for x in g.get("allowed_namespaces") or ["store"]]}
    cls = {"type": "string", "enum": [str(x) for x in g.get("allowed_db_instance_classes") or ["db.t4g.micro"]]}
    variants = [
        _variant("flagsmith.set_flag", {"flag": S, "enabled": {"type": "boolean"}}, ["flag", "enabled"]),
        _variant("flagsmith.set_value", {"flag": S, "value": {"type": "string", "description": "JSON as a string"}},
                 ["flag", "value"]),
        _variant("k8s.scale", {"namespace": ns, "deployment": S, "replicas": {"type": "integer"}},
                 ["namespace", "deployment", "replicas"]),
        _variant("k8s.set_resources", {"namespace": ns, "deployment": S, "container": S,
                                       "cpu_limit": {"type": "string", "description": 'e.g. "1" or "750m"'},
                                       "memory_limit": {"type": "string", "description": 'e.g. "1Gi" or "768Mi"'},
                                       "cpu_request": S, "memory_request": S},
                 ["namespace", "deployment", "container", "cpu_limit", "memory_limit"]),
        _variant("db.set_pool", {"pool_size": {"type": "integer"}, "max_overflow": {"type": "integer"},
                                 "timeout_s": {"type": "number"}}, ["pool_size", "max_overflow", "timeout_s"]),
        _variant("aws.rds.create_read_replica", {"source_db_id": S, "replica_id": S, "instance_class": cls},
                 ["source_db_id", "replica_id", "instance_class"]),
        _variant("aws.rds.modify_instance_class", {"db_id": S, "instance_class": cls}, ["db_id", "instance_class"]),
        _variant("nudgebee.run_workflow", {"workflow_id": S, "inputs": {"type": "string",
                                                                        "description": "JSON object as a string"}},
                 ["workflow_id", "inputs"]),
        _variant("notify.founder", {"text_en": S}, ["text_en"]),
    ]
    option = {"type": "object", "properties": {
        "key": {"type": "string", "enum": ["recommended", "alt1", "alt2", "reject"]},
        "title_en": {"type": "string", "description": "<= 40 chars, plain words"},
        "impact_en": S, "cost_en": S,
        "actions": {"type": "array", "items": {"anyOf": variants}}},
        "required": ["key", "title_en", "impact_en", "cost_en", "actions"], "additionalProperties": False}
    tool: dict[str, Any] = {
        "name": PLAN_TOOL,
        "description": "Submit the remediation plan for the founder to approve. Call exactly once.",
        "input_schema": {"type": "object", "properties": {
            "founder_summary_en": {"type": "string", "description": "<= 300 chars, plain words, no jargon"},
            "technical_summary": S,
            "options": {"type": "array", "description": "1-3 options; the last is always key=reject",
                        "items": option}},
            "required": ["founder_summary_en", "technical_summary", "options"], "additionalProperties": False},
    }
    if strict:
        tool["strict"] = True
    return tool


def validate_draft(data: Any, g: dict, max_actions: int) -> PlanDraft:
    if not isinstance(data, dict):
        raise PlanInvalid(f"tool input is not an object: {type(data).__name__}")
    try:
        draft = PlanDraft.model_validate(data)
    except ValidationError as e:
        raise PlanInvalid(f"schema: {e.errors(include_url=False)[:3]}") from e
    opts = []
    for o in draft.options:
        try:
            opts.append(o.model_copy(update={"actions": validate_actions(o.actions, g, max_actions)}))
        except GuardrailError as e:
            raise PlanInvalid(f"option {o.key!r}: {e}") from e
    return draft.model_copy(update={"options": opts})


ASK_PROMPT = """You are Insidious's ops assistant for a small Indian D2C store run by a NON-technical founder.
Answer the founder's question using ONLY the telemetry provided. Use plain words (no jargon like pods, p95, replicas, CPU, Kubernetes), at most 3 short sentences, numbers rounded. If a plan is waiting for approval, mention it in one sentence. Reply in English; another system translates it."""


class Planner:
    def __init__(self, settings: Settings, claude_client: Any = None):
        self.s = settings
        self._injected = claude_client
        self._clients: dict[str, Any] = {}

    # ------------------------------------------------------------ provider plumbing
    def _client(self, provider: str) -> Any:
        if self._injected is not None:
            return self._injected
        key = f"{provider}:{self.s.bedrock_api}"
        if key not in self._clients:
            if provider == "bedrock":
                if self.s.bedrock_api == "invoke":  # legacy bedrock-runtime InvokeModel path
                    self._clients[key] = anthropic.AsyncAnthropicBedrock(aws_region=self.s.aws_region, timeout=120.0,
                                                                         max_retries=2)
                else:  # Messages API on Bedrock (Mantle), the recommended Bedrock client
                    self._clients[key] = anthropic.AsyncAnthropicBedrockMantle(aws_region=self.s.aws_region,
                                                                               timeout=120.0, max_retries=2)
            else:
                if not self.s.anthropic_api_key:
                    raise PlannerUnavailable("ANTHROPIC_API_KEY is not set")
                self._clients[key] = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key, timeout=120.0,
                                                              max_retries=2)
        return self._clients[key]

    def _model(self, rt: AgentRuntime) -> str:
        if rt.provider == "bedrock":
            if self.s.bedrock_model:
                return self.s.bedrock_model
            return rt.model if "." in rt.model else f"anthropic.{rt.model}"
        return rt.model

    async def _create(self, client: Any, rt: AgentRuntime, kwargs: dict, use_fallbacks: bool) -> Any:
        if rt.effort:
            kwargs["output_config"] = {"effort": rt.effort}
        if use_fallbacks and rt.provider == "anthropic" and rt.model in SERVER_FALLBACK_MODELS:
            # Server-side refusal fallback: on a policy decline the API re-runs the request on a fallback model.
            return await client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return await client.messages.create(**kwargs)

    @staticmethod
    def _tool_input(resp: Any) -> Any:
        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            raise PlanInvalid(f"model refused ({getattr(details, 'category', None)})")
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == PLAN_TOOL:
                inp = block.input
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except ValueError as e:
                        raise PlanInvalid("tool input is not valid JSON") from e
                return inp
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise PlanInvalid("output truncated (max_tokens)")
        raise PlanInvalid("no submit_plan tool call in the response")

    # ------------------------------------------------------------ planning
    async def make_plan(self, incident: dict, telemetry: dict, facts: dict, rt: AgentRuntime) -> tuple[PlanDraft, dict]:
        if self.s.mock:
            return rule_plan(facts, self.s), {"planner": "rules", "model": None}
        try:
            draft = await self._claude_plan(incident, telemetry, facts, rt)
            return draft, {"planner": "claude", "model": self._model(rt), "provider": rt.provider}
        except (PlanInvalid, PlannerUnavailable) as e:
            log("planner_fallback", reason=str(e)[:300])
            return rule_plan(facts, self.s), {"planner": "rules-fallback", "model": None, "fallback_reason": str(e)[:300]}
        except Exception as e:  # noqa: BLE001 - never let the planner crash the incident flow
            log("planner_error", error=f"{type(e).__name__}: {str(e)[:300]}")
            return rule_plan(facts, self.s), {"planner": "rules-fallback", "model": None,
                                              "fallback_reason": f"{type(e).__name__}"}

    def _user_content(self, incident: dict, telemetry: dict, facts: dict, rt: AgentRuntime) -> str:
        ctx = {
            "namespace": self.s.namespace, "deployment": self.s.deployment,
            "database_rds_id": self.s.rds_id or None, "max_actions_per_option": rt.max_actions,
            "language_hint": rt.language_hint,
        }
        return (f"<context>{json.dumps(ctx)}</context>\n"
                f"<incident>{json.dumps(incident, default=str)[:4000]}</incident>\n"
                f"<facts>{json.dumps(facts, default=str)}</facts>\n"
                f"<telemetry>{prompt_json(telemetry)}</telemetry>\n"
                "Propose the plan now by calling submit_plan.")

    async def _claude_plan(self, incident: dict, telemetry: dict, facts: dict, rt: AgentRuntime) -> PlanDraft:
        client = self._client(rt.provider)
        g = self.s.guardrails
        system = SYSTEM_PROMPT.replace("{max_actions}", str(rt.max_actions)).replace("{catalog}", describe_catalog(g))
        content = self._user_content(incident, telemetry, facts, rt)
        strict, use_fallbacks = True, True
        last_error = ""
        for attempt in (1, 2):
            user = content if attempt == 1 else (
                f"{content}\n\nYour previous answer was rejected: {last_error}\n"
                "Fix it and call submit_plan again, following every rule.")
            kwargs: dict[str, Any] = {
                "model": self._model(rt),
                "max_tokens": 16000,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": [plan_tool(g, strict=strict)],
                # Forced tool_choice (any/tool) returns 400 on Claude Sonnet 5.5 / Opus 5.5; steer with the prompt
                # and keep schema-valid arguments with strict tool use instead.
                "tool_choice": {"type": "auto"},
                "messages": [{"role": "user", "content": user}],
            }
            try:
                resp = await self._create(client, rt, kwargs, use_fallbacks)
                log("claude_plan_response", attempt=attempt, model=kwargs["model"],
                    stop_reason=getattr(resp, "stop_reason", None), request_id=getattr(resp, "_request_id", None))
                return validate_draft(self._tool_input(resp), g, rt.max_actions)
            except PlanInvalid as e:
                last_error = str(e)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
                raise PlannerUnavailable(f"{type(e).__name__}: {getattr(e, 'message', str(e))[:200]}") from e
            except anthropic.BadRequestError as e:
                # e.g. a schema feature strict mode rejects, or fallbacks unsupported: retry plainer.
                last_error = f"bad request: {getattr(e, 'message', str(e))[:200]}"
                strict, use_fallbacks = False, False
            except anthropic.RateLimitError as e:
                last_error = f"rate limited: {getattr(e, 'message', '')[:100]}"
            except anthropic.APIStatusError as e:
                last_error = f"API error {e.status_code}"
            except anthropic.APIConnectionError as e:
                last_error = f"connection error: {type(e).__name__}"
            log("claude_plan_attempt_failed", attempt=attempt, error=last_error[:300])
        raise PlanInvalid(f"Claude plan invalid after retry: {last_error}")

    # ------------------------------------------------------------ Q&A
    async def answer(self, question: str, telemetry: dict, facts: dict, latest_plan: dict | None,
                     rt: AgentRuntime) -> tuple[str, str]:
        if self.s.mock or not rt.ai_agent:
            return rule_answer(question, facts, latest_plan), "rules"
        plan_brief = None
        if latest_plan:
            plan_brief = {k: latest_plan.get(k) for k in ("id", "status", "founder_summary_en", "results")}
            plan_brief["options"] = [{k: o.get(k) for k in ("key", "title_en", "cost_en")}
                                     for o in latest_plan.get("options") or []]
        user = (f"<facts>{json.dumps(facts, default=str)}</facts>\n<telemetry>{prompt_json(telemetry, 16000)}"
                f"</telemetry>\n<latest_plan>{json.dumps(plan_brief, default=str)}</latest_plan>\n"
                f"<question>{question[:1000]}</question>")
        try:
            client = self._client(rt.provider)
            kwargs: dict[str, Any] = {"model": self._model(rt), "max_tokens": 8000, "system": ASK_PROMPT,
                                      "messages": [{"role": "user", "content": user}]}
            resp = await self._create(client, rt, kwargs, use_fallbacks=True)
            if getattr(resp, "stop_reason", None) == "refusal":
                raise PlanInvalid("refused")
            text = " ".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text").strip()
            if text:
                return text[:1200], "claude"
        except Exception as e:  # noqa: BLE001 - fall back to the rule answer
            log("ask_fallback", error=f"{type(e).__name__}: {str(e)[:200]}")
        return rule_answer(question, facts, latest_plan), "rules-fallback"
