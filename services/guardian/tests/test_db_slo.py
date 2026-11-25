"""Database pool SLO (rule insidious-db, route key db:primary) and the AI-agent incident hand-off."""
import json

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import fmt_seconds
from app.main import create_app
from app.prom import db_utilisation_query, db_wait_p95_query

from .conftest import AGENT, T0, webhook_payloads


async def ticks(g, n):
    for _ in range(n):
        await g.tick()


def saturate(ps, in_use=5, size=5, wait=0.4):
    ps.db_in_use, ps.db_size, ps.db_wait_s = in_use, size, wait


def healthy(ps):
    ps.db_in_use, ps.db_size, ps.db_wait_s = 1, 5, 0.002


def agent_bodies(router) -> list[dict]:
    return [json.loads(c.request.content) for c in router.agent.calls]


def test_db_promql():
    assert db_utilisation_query() == (
        'sum(insidious_db_pool_in_use{db="primary"}) / clamp_min(sum(insidious_db_pool_size{db="primary"}), 1)')
    assert db_wait_p95_query("primary", "30s") == (
        'histogram_quantile(0.95, sum(rate(insidious_db_pool_wait_seconds_bucket{db="primary"}[30s])) by (le))')
    assert fmt_seconds(0.4) == "0.4s" and fmt_seconds(0.25) == "0.25s" and fmt_seconds(1.234) == "1.2s"


def test_settings_read_database_block(tmp_path):
    s = Settings.from_env({"database": {"slo": {"pool_utilisation": 0.8, "wait_p95_s": 0.5}}})
    assert s.db_target_utilisation == 0.8 and s.db_target_wait_p95_s == 0.5 and s.db_slo_enabled
    d = Settings.from_env({})
    assert d.db_target_utilisation == 0.9 and d.db_target_wait_p95_s == 0.25


async def test_db_saturation_fires_after_three_samples_and_resolves(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.rps = 55.0
    saturate(prom_state)
    await ticks(g, 2)
    assert mocked.webhook.call_count == 0
    assert g.snapshot()["db"]["primary"]["breach_count"] == 2
    await g.tick()
    assert mocked.webhook.call_count == 1
    p = webhook_payloads(mocked)[0]
    assert p["id"] == f"slo-db-primary-{int(T0)}"
    assert p["fingerprint"] == "jeffy/store-api/db-primary"
    assert p["status"] == "firing" and p["rule_name"] == "insidious-db" and p["severity"] == "high"
    assert p["title"] == "Database connections 5/5 busy, shoppers waiting 0.4s"
    assert p["subject"] == {"name": "store-api", "type": "Deployment", "namespace": "store"}
    labels = p["labels"]
    assert labels["route"] == "db:primary" and labels["pool_in_use"] == "5" and labels["pool_size"] == "5"
    assert labels["pool_utilisation"] == "1.00" and labels["wait_p95_s"] == "0.400" and labels["rps"] == "55"
    assert labels["read_from_replica"] == "off"
    assert all(isinstance(v, str) for v in labels.values())
    st = g.snapshot()
    db = st["db"]["primary"]
    assert db["utilisation"] == 1.0 and db["wait_p95_s"] == 0.4 and db["breaching"] is True
    assert db["target_utilisation"] == 0.9 and db["target_wait_p95_s"] == 0.25 and db["route"] == "db:primary"
    assert st["incident"]["id"] == p["id"] and st["incident"]["route"] == "db:primary"
    assert st["incident"]["rule_name"] == "insidious-db" and st["incident"]["kind"] == "db"
    assert any(f["text"].startswith("DB pool saturated") for f in st["feed"])
    assert st["history"][-1]["db"]["utilisation"] == 1.0
    # the latency SLO stayed healthy: only the DB incident fired
    assert st["slo"]["/checkout"]["breaching"] is False

    await ticks(g, 2)
    assert mocked.webhook.call_count == 1  # no re-send while firing
    healthy(prom_state)
    await ticks(g, 2)
    assert mocked.webhook.call_count == 1
    await g.tick()
    assert mocked.webhook.call_count == 2
    res = webhook_payloads(mocked)[1]
    assert res["status"] == "resolved" and res["id"] == p["id"] and res["fingerprint"] == p["fingerprint"]
    assert res["rule_name"] == "insidious-db"
    assert res["title"].startswith("Database recovered")
    assert g.snapshot()["incident"]["status"] == "resolved"


async def test_wait_alone_breaches(mocked, prom_state, make_guardian):
    g = make_guardian()
    saturate(prom_state, in_use=2, size=5, wait=0.3)  # utilisation 0.4 is fine, wait 0.3 s > 0.25 s
    await ticks(g, 3)
    p = webhook_payloads(mocked)[0]
    assert p["rule_name"] == "insidious-db" and p["title"] == "Database connections 2/5 busy, shoppers waiting 0.3s"


async def test_utilisation_alone_breaches_at_threshold(mocked, prom_state, make_guardian):
    g = make_guardian()
    saturate(prom_state, in_use=9, size=10, wait=None)  # 0.9 >= 0.9, no wait data (no traffic in the window)
    await ticks(g, 3)
    p = webhook_payloads(mocked)[0]
    assert p["title"] == "Database connections 9/10 busy" and p["labels"]["wait_p95_s"] == "n/a"


async def test_db_no_data_never_fires_or_recovers(mocked, prom_state, make_guardian):
    g = make_guardian()
    await ticks(g, 4)  # store without DB metrics
    st = g.snapshot()
    assert st["db"]["primary"]["utilisation"] is None and st["db"]["primary"]["breach_count"] == 0
    assert st["db"]["replica"] is None and mocked.webhook.call_count == 0
    saturate(prom_state)
    await ticks(g, 3)
    assert mocked.webhook.call_count == 1
    # the pod stops answering scrapes: series vanish. That must not count as recovery.
    prom_state.db_in_use = prom_state.db_size = prom_state.db_wait_s = None
    await ticks(g, 5)
    assert mocked.webhook.call_count == 1
    assert g.snapshot()["incident"]["status"] == "firing"
    prom_state.down = True
    await ticks(g, 4)
    assert mocked.webhook.call_count == 1 and g.snapshot()["incident"]["status"] == "firing"


async def test_db_disarmed_and_disabled(mocked, prom_state, make_guardian):
    g = make_guardian(armed=False)
    saturate(prom_state)
    await ticks(g, 4)
    assert mocked.webhook.call_count == 0 and g.snapshot()["db"]["primary"]["breaching"] is True
    g2 = make_guardian(db_slo_enabled=False)
    await ticks(g2, 4)
    assert mocked.webhook.call_count == 0 and g2.snapshot()["db"] is None


async def test_replica_stats_in_state(mocked, prom_state, make_guardian):
    g = make_guardian()
    healthy(prom_state)
    prom_state.replica_in_use, prom_state.replica_size = 2, 5
    await g.tick()
    db = g.snapshot()["db"]
    assert db["replica"] == {"in_use": 2.0, "size": 5.0} and db["read_from_replica"] is True
    prom_state.replica_size = 0  # SDK zeroes the gauge when reads go back to the primary
    await g.tick()
    assert g.snapshot()["db"]["replica"] is None


async def test_both_incidents_posted_to_agent(mocked, prom_state, make_guardian):
    g = make_guardian(agent_url=AGENT, internal_token="tok-123")
    prom_state.p95_s, prom_state.rps = 1.84, 420.0
    saturate(prom_state)
    await ticks(g, 3)
    assert mocked.webhook.call_count == 2
    assert mocked.agent.call_count == 2
    bodies = agent_bodies(mocked)
    for call in mocked.agent.calls:
        assert call.request.headers["X-Insidious-Token"] == "tok-123"
    kinds = {b["incident"]["rule_name"]: b for b in bodies}
    assert set(kinds) == {"insidious-slo", "insidious-db"}
    lat, db = kinds["insidious-slo"], kinds["insidious-db"]
    for b in bodies:
        assert b["source"] == "guardian" and b["incident"]["status"] == "firing"
        for key in ("armed", "slo", "rps", "replicas", "flags", "incident", "db", "history", "feed"):
            assert key in b["state"]
    assert lat["incident"]["id"] == f"slo-checkout-p95-{int(T0)}" and lat["incident"]["kind"] == "latency"
    assert lat["incident"]["title"] == "Checkout p95 1840ms breaches 800ms SLO at 420 rps"
    assert db["incident"]["route"] == "db:primary"
    assert db["incident"]["title"] == "Database connections 5/5 busy, shoppers waiting 0.4s"
    assert db["state"]["db"]["primary"]["utilisation"] == 1.0
    st = g.snapshot()
    assert st["incident"]["agent_plan_id"] == "plan-20261125-140205-ab12"
    assert any("AI ops agent" in f["text"] for f in st["feed"])
    # resolution is not re-posted; a later re-fire is
    prom_state.p95_s = 0.2
    healthy(prom_state)
    await ticks(g, 3)
    assert mocked.agent.call_count == 2


async def test_agent_failures_are_best_effort(mocked, prom_state, make_guardian, capsys):
    mocked.agent.mock(side_effect=httpx.ReadTimeout("slow agent"))
    g = make_guardian(agent_url=AGENT)
    saturate(prom_state)
    await ticks(g, 3)
    assert mocked.agent.call_count == 1 and mocked.webhook.call_count == 1
    assert g.snapshot()["incident"]["status"] == "firing"
    assert "agent_incident_error" in capsys.readouterr().out
    assert "X-Insidious-Token" not in mocked.agent.calls[0].request.headers  # no token configured

    mocked.agent.mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
    g2 = make_guardian(agent_url=AGENT, internal_token="wrong")
    prom_state.p95_s = 2.0
    await ticks(g2, 3)
    assert mocked.agent.call_count >= 2
    assert "agent_incident_failed" in capsys.readouterr().out


async def test_agent_skipped_without_url(mocked, prom_state, make_guardian):
    g = make_guardian()
    saturate(prom_state)
    await ticks(g, 3)
    assert mocked.webhook.call_count == 1 and mocked.agent.call_count == 0


def test_state_endpoint_has_db(mocked, prom_state, make_guardian):
    g = make_guardian()
    app = create_app(guardian=g, start_loop=False)
    with TestClient(app) as c:
        st = c.get("/api/state").json()
        assert set(st["db"]) == {"primary", "replica", "read_from_replica"}
        assert set(st["db"]["primary"]) >= {"in_use", "size", "utilisation", "wait_p95_s", "breaching",
                                            "breach_count", "target_utilisation", "target_wait_p95_s"}
