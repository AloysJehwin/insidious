import copy

import anthropic
import httpx2

from app.config import Settings
from app.flags import AgentRuntime
from app.models import PlanDraft
from app.planner import Planner, plan_tool, rule_plan

from .conftest import FakeClaude, make_settings, text_response, tool_use_response

CPU_FACTS = {"kind": "cpu", "p95_ms": 1840.0, "target_p95_ms": 800, "rps": 55.0, "replicas": 1,
             "cpu_limit": "500m", "memory_limit": "512Mi", "container": "store-api", "cpu_pct_of_limit": 98.0,
             "heavy_features": True, "route": "/checkout", "latency_breaching": True}
DB_FACTS = {"kind": "db", "db_utilisation": 1.0, "db_wait_p95_s": 0.4, "db_pool_size": 5, "rds_class": "db.t4g.micro",
            "rds_replicas": [], "p95_ms": 1500.0, "rps": 40.0, "replicas": 1}

VALID_CLAUDE_PLAN = {
    "founder_summary_en": "Lots of shoppers and checkout is slow. I suggest pausing product suggestions and adding "
                          "2 more servers, about ₹8 more per hour.",
    "technical_summary": "store-api p95 1840ms at 55 rps, CPU 98% of 500m.",
    "options": [
        {"key": "recommended", "title_en": "Pause suggestions + add 2 servers", "impact_en": "Fast in a minute",
         "cost_en": "About ₹8 more per hour", "actions": [
             {"type": "flagsmith.set_flag", "params": {"flag": "heavy_features", "enabled": False}},
             {"type": "k8s.scale", "params": {"namespace": "store", "deployment": "store-api", "replicas": 3}}]},
        {"key": "alt1", "title_en": "Only pause suggestions", "impact_en": "Faster soon", "cost_en": "No extra cost",
         "actions": [{"type": "flagsmith.set_flag", "params": {"flag": "heavy_features", "enabled": False}}]},
        {"key": "reject", "title_en": "Do nothing for now", "impact_en": "", "cost_en": "", "actions": []},
    ],
}


def rt(**kw) -> AgentRuntime:
    return AgentRuntime(provider="anthropic", model="claude-sonnet-5-5", **kw)


# ------------------------------------------------------------------ rule-based (AGENT_MOCK=1)
def test_mock_cpu_plan(tmp_path):
    p = rule_plan(CPU_FACTS, make_settings(tmp_path))
    assert [o.key for o in p.options] == ["recommended", "alt1", "reject"]
    rec, alt, rej = p.options
    assert [(a.type, a.params) for a in rec.actions] == [
        ("flagsmith.set_flag", {"flag": "heavy_features", "enabled": False}),
        ("k8s.scale", {"namespace": "store", "deployment": "store-api", "replicas": 3}),
    ]
    assert rec.cost_en == "About ₹8 more per hour"
    assert alt.actions[0].type == "k8s.set_resources"
    assert alt.actions[0].params["cpu_limit"] == "1" and alt.actions[0].params["memory_limit"] == "1Gi"
    assert rej.title_en == "Do nothing for now" and rej.actions == []
    assert len(p.founder_summary_en) <= 300 and len(rec.title_en) <= 40
    for jargon in ("pod", "p95", "replica", "CPU"):
        assert jargon not in p.founder_summary_en


def test_mock_cpu_plan_respects_max_replicas(tmp_path):
    s = make_settings(tmp_path)
    s.guardrails["max_replicas"] = 2
    p = rule_plan(CPU_FACTS, s)
    assert p.options[0].actions[1].params["replicas"] == 2


def test_mock_db_plan_with_rds_creates_replica(tmp_path):
    p = rule_plan(DB_FACTS, make_settings(tmp_path, rds_id="insidious-db"))
    rec, alt, rej = p.options
    assert [a.type for a in rec.actions] == ["db.set_pool", "aws.rds.create_read_replica"]
    assert rec.actions[0].params == {"pool_size": 10, "max_overflow": 5, "timeout_s": 5.0}
    assert rec.actions[1].params == {"source_db_id": "insidious-db", "replica_id": "insidious-db-read-1",
                                     "instance_class": "db.t4g.micro"}
    assert rec.cost_en == "About ₹1.5 more per hour"
    assert alt.actions[0].type == "aws.rds.modify_instance_class"
    assert alt.actions[0].params == {"db_id": "insidious-db", "instance_class": "db.t4g.small"}
    assert rej.key == "reject"


def test_mock_db_plan_without_rds_uses_replica_flag(tmp_path):
    facts = dict(DB_FACTS, rds_replicas=[])
    p = rule_plan(facts, make_settings(tmp_path, rds_id=""))
    assert [(a.type, a.params.get("flag")) for a in p.options[0].actions] == [
        ("db.set_pool", None), ("flagsmith.set_flag", "read_from_replica")]
    assert p.options[0].actions[1].params["enabled"] is True


def test_mock_db_plan_existing_replica_turns_flag_on(tmp_path):
    p = rule_plan(dict(DB_FACTS, rds_replicas=["insidious-db-read-1"]), make_settings(tmp_path, rds_id="insidious-db"))
    assert p.options[0].actions[1].type == "flagsmith.set_flag"


async def test_mock_mode_never_calls_claude(tmp_path):
    claude = FakeClaude()
    draft, meta = await Planner(make_settings(tmp_path, mock=True), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "rules" and claude.calls == []


# ------------------------------------------------------------------ Claude path
async def test_claude_tool_use_gives_validated_plan(tmp_path):
    claude = FakeClaude(tool_use_response(copy.deepcopy(VALID_CLAUDE_PLAN)))
    planner = Planner(make_settings(tmp_path, mock=False), claude)
    draft, meta = await planner.make_plan({"id": "x"}, {"guardian": {}}, CPU_FACTS, rt())
    assert meta == {"planner": "claude", "model": "claude-sonnet-5-5", "provider": "anthropic"}
    assert isinstance(draft, PlanDraft) and [o.key for o in draft.options] == ["recommended", "alt1", "reject"]
    assert draft.options[0].actions[1].params["replicas"] == 3
    call = claude.calls[0]
    assert call["beta"] is True and call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["model"] == "claude-sonnet-5-5" and call["tool_choice"] == {"type": "auto"}
    assert call["tools"][0]["name"] == "submit_plan" and call["tools"][0]["strict"] is True
    assert "temperature" not in call and "thinking" not in call
    assert "Do nothing for now" in call["system"][0]["text"]


async def test_claude_invalid_then_valid_retries_once(tmp_path):
    bad = copy.deepcopy(VALID_CLAUDE_PLAN)
    bad["options"][0]["actions"][1]["params"]["replicas"] = 12  # breaks max_replicas
    claude = FakeClaude(tool_use_response(bad), tool_use_response(copy.deepcopy(VALID_CLAUDE_PLAN)))
    draft, meta = await Planner(make_settings(tmp_path, mock=False), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "claude" and len(claude.calls) == 2
    retry_msg = claude.calls[1]["messages"][0]["content"]
    assert "previous answer was rejected" in retry_msg and "replicas=12" in retry_msg


async def test_claude_invalid_twice_falls_back_to_rules(tmp_path):
    no_reject = copy.deepcopy(VALID_CLAUDE_PLAN)
    no_reject["options"] = no_reject["options"][:2]  # last option is not reject
    claude = FakeClaude(tool_use_response(no_reject), text_response("Here is my plan in prose."))
    draft, meta = await Planner(make_settings(tmp_path, mock=False), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "rules-fallback" and len(claude.calls) == 2
    assert "no submit_plan tool call" in meta["fallback_reason"]
    assert draft.options[0].actions[0].params == {"flag": "heavy_features", "enabled": False}


async def test_claude_refusal_and_bad_action_type_fall_back(tmp_path):
    bad = copy.deepcopy(VALID_CLAUDE_PLAN)
    bad["options"][0]["actions"] = [{"type": "shell.exec", "params": {"cmd": "kubectl delete ns store"}}]
    claude = FakeClaude(tool_use_response(bad), tool_use_response({}, stop_reason="refusal"))
    draft, meta = await Planner(make_settings(tmp_path, mock=False), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "rules-fallback" and "refused" in meta["fallback_reason"]


async def test_claude_auth_error_falls_back_without_retry(tmp_path):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None)
    claude = FakeClaude(err)
    draft, meta = await Planner(make_settings(tmp_path, mock=False), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "rules-fallback" and len(claude.calls) == 1


async def test_claude_bad_request_retries_without_strict_or_fallbacks(tmp_path):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("schema too complex", response=httpx2.Response(400, request=req), body=None)
    claude = FakeClaude(err, tool_use_response(copy.deepcopy(VALID_CLAUDE_PLAN)))
    draft, meta = await Planner(make_settings(tmp_path, mock=False), claude).make_plan({}, {}, CPU_FACTS, rt())
    assert meta["planner"] == "claude"
    assert claude.calls[1]["beta"] is False and "strict" not in claude.calls[1]["tools"][0]


async def test_no_api_key_falls_back(tmp_path):
    s = make_settings(tmp_path, mock=False, anthropic_api_key="")
    draft, meta = await Planner(s).make_plan({}, {}, DB_FACTS, rt())
    assert meta["planner"] == "rules-fallback" and "ANTHROPIC_API_KEY" in meta["fallback_reason"]
    assert draft.options[0].actions[0].type == "db.set_pool"


def test_bedrock_model_id_prefix(tmp_path):
    p = Planner(make_settings(tmp_path))
    assert p._model(AgentRuntime(provider="bedrock", model="claude-sonnet-5-5")) == "anthropic.claude-sonnet-5-5"
    assert p._model(AgentRuntime(provider="bedrock", model="global.anthropic.x")) == "global.anthropic.x"


def test_plan_tool_schema_is_strict_compatible():
    tool = plan_tool(Settings().guardrails)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) <= set(node["properties"])
            for bad in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
                assert bad not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tool["input_schema"])


async def test_ask_uses_claude_text(tmp_path):
    claude = FakeClaude(text_response("Checkout is slow; I suggested adding 2 servers."))
    answer, src = await Planner(make_settings(tmp_path, mock=False), claude).answer(
        "kya chal raha hai?", {}, CPU_FACTS, None, rt())
    assert src == "claude" and "2 servers" in answer
    assert "tools" not in claude.calls[0]
