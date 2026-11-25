import json

import httpx
from fastapi.testclient import TestClient

from app.engine import route_slug, route_title
from app.main import create_app
from app.prom import error_rate_query, p95_query, rps_query

from .conftest import NB_API, T0, webhook_payloads


async def ticks(g, n):
    for _ in range(n):
        await g.tick()


# ------------------------------------------------------------------ PromQL
def test_promql_matches_contract_exactly():
    assert p95_query("/checkout") == (
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{route="/checkout"}[30s])) by (le))')
    assert error_rate_query("/checkout") == (
        'sum(rate(http_requests_total{route="/checkout",status=~"5.."}[30s])) '
        '/ clamp_min(sum(rate(http_requests_total{route="/checkout"}[30s])), 1e-9)')
    assert rps_query() == "sum(rate(http_requests_total[30s]))"


def test_promql_api_alias():
    q = p95_query("/checkout", "30s", api_alias=True)
    assert 'route=~"/checkout|/api/checkout"' in q
    assert route_slug("/checkout") == "checkout" and route_slug("/") == "home"
    assert route_title("/checkout") == "Checkout" and route_title("/") == "Home"


# -------------------------------------------------------- breach / resolve
async def test_breach_fires_after_three_samples(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.p95_s, prom_state.rps = 1.84, 420.0
    await ticks(g, 2)
    assert mocked.webhook.call_count == 0
    assert g.snapshot()["slo"]["/checkout"]["breach_count"] == 2
    await g.tick()
    assert mocked.webhook.call_count == 1
    p = webhook_payloads(mocked)[0]
    assert p["id"] == f"slo-checkout-p95-{int(T0)}"  # unix ts of the FIRST breach
    assert p["fingerprint"] == "jeffy/store-api/checkout-p95"
    assert p["status"] == "firing"
    assert p["rule_name"] == "insidious-slo" and p["severity"] == "high"
    assert p["subject"] == {"name": "store-api", "type": "Deployment", "namespace": "store"}
    assert p["title"] == "Checkout p95 1840ms breaches 800ms SLO at 420 rps"
    assert p["labels"]["route"] == "/checkout" and p["labels"]["p95_ms"] == "1840"
    assert p["labels"]["rps"] == "420" and p["labels"]["heavy_features"] == "on"
    assert all(isinstance(v, str) for v in p["labels"].values())
    assert mocked.webhook.calls[0].request.headers["Authorization"] == "Bearer abc123"
    # stays firing without re-sending
    await ticks(g, 2)
    assert mocked.webhook.call_count == 1
    st = g.snapshot()
    assert st["incident"]["status"] == "firing" and st["incident"]["id"] == p["id"]
    assert any("SLO breach" in f["text"] for f in st["feed"])


async def test_recovery_sends_resolved_with_same_id(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.p95_s = 1.5
    await ticks(g, 3)
    fired = webhook_payloads(mocked)[0]
    prom_state.p95_s = 0.3
    await ticks(g, 2)
    assert mocked.webhook.call_count == 1
    await g.tick()
    assert mocked.webhook.call_count == 2
    res = webhook_payloads(mocked)[1]
    assert res["status"] == "resolved"
    assert res["id"] == fired["id"] and res["fingerprint"] == fired["fingerprint"]
    st = g.snapshot()
    assert st["incident"]["status"] == "resolved"
    assert any(f["text"].startswith("Recovered") for f in st["feed"])
    # a new breach later opens a new incident with a new id
    prom_state.p95_s = 2.0
    await ticks(g, 3)
    assert mocked.webhook.call_count == 3
    assert webhook_payloads(mocked)[2]["id"] != fired["id"]


async def test_error_rate_breach_fires(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.p95_s, prom_state.err = 0.1, 0.25
    await ticks(g, 3)
    p = webhook_payloads(mocked)[0]
    assert "error rate 25.0%" in p["title"]


async def test_dry_run_does_not_send(mocked, prom_state, make_guardian, capsys):
    g = make_guardian(nudgebee_dry_run=True)
    prom_state.p95_s = 1.2
    await ticks(g, 3)
    assert mocked.webhook.call_count == 0
    st = g.snapshot()
    assert st["incident"]["status"] == "firing" and st["incident"]["dry_run"] is True
    assert "(dry-run)" in st["feed"][-1]["text"]
    assert "nudgebee_event_dry_run" in capsys.readouterr().out


async def test_disarmed_does_not_fire_until_armed(mocked, prom_state, make_guardian):
    g = make_guardian(armed=False)
    prom_state.p95_s = 1.2
    await ticks(g, 4)
    assert mocked.webhook.call_count == 0
    assert g.snapshot()["slo"]["/checkout"]["breaching"] is True
    g.armed = True
    await g.tick()
    assert mocked.webhook.call_count == 1


async def test_readiness_gain_pickup(mocked, prom_state, make_guardian, tmp_path):
    (tmp_path / "readiness-20261125-1200.json").write_text(json.dumps({"capacity_gain_if_shed": "+10%"}))
    (tmp_path / "readiness-20261125-1340.json").write_text(json.dumps({
        "capacity_gain_if_shed": "+62%", "pass1": {"breaking_rps": 420}, "pass2": {"breaking_rps": 680}}))
    g = make_guardian()
    prom_state.p95_s = 1.84
    await ticks(g, 3)
    p = webhook_payloads(mocked)[0]
    assert p["labels"]["capacity_gain_if_shed"] == "+62%"
    assert "420 → 680" in p["description"]


async def test_no_readiness_report_is_unknown(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.p95_s = 1.84
    await ticks(g, 3)
    assert webhook_payloads(mocked)[0]["labels"]["capacity_gain_if_shed"] == "unknown"


# ------------------------------------------------------ degraded sources
async def test_prometheus_down_is_graceful(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.down = True
    await ticks(g, 4)
    st = g.snapshot()
    assert st["slo"]["/checkout"]["p95_ms"] is None
    assert st["slo"]["/checkout"]["breach_count"] == 0
    assert st["prometheus_ok"] is False and st["replicas"] == {"store-api": None}
    assert mocked.webhook.call_count == 0
    assert len(st["history"]) == 4


async def test_flags_and_replicas_from_store_and_prom(mocked, prom_state, make_guardian):
    g = make_guardian()
    prom_state.replicas = 3
    await g.tick()
    st = g.snapshot()
    assert st["flags"]["heavy_features"] is True
    assert st["replicas"] == {"store-api": 3}
    assert st["rps"] == 100.0


# ------------------------------------------------------ last_execution
def _gql_handler(fail_list: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        q = json.loads(request.content)["query"]
        if "workflow_list_executions" in q:
            if fail_list or "rows" not in q:
                return httpx.Response(200, json={"errors": [{"message": "Cannot query field"}]})
            return httpx.Response(200, json={"data": {"workflow_list_executions": {"rows": [
                {"id": "ex-old", "status": "COMPLETED", "start_time": "2026-11-25T13:00:00Z"},
                {"id": "ex-1", "status": "RUNNING", "start_time": "2026-11-25T14:02:10Z"}]}}})
        if "workflow_get_execution" in q:
            assert '"ex-1"' in q and '"wf-1"' in q
            return httpx.Response(200, json={"data": {"workflow_get_execution": {
                "id": "ex-1", "status": "RUNNING", "start_time": "2026-11-25T14:02:10Z", "close_time": None,
                "error": None, "tasks": [{"id": "t1", "type": "llm.classify", "status": "COMPLETED"},
                                         {"id": "t2", "type": "core.approval", "status": "WAITING"}]}}})
        return httpx.Response(200, json={"errors": [{"message": "unknown"}]})
    return handler


NB_KW = dict(nudgebee_url=NB_API, nudgebee_token="sk-test-token", nudgebee_account_id="acc-1",
             nudgebee_wf_incident_id="wf-1")


async def test_last_execution_polled_during_incident(mocked, prom_state, make_guardian):
    gql = mocked.post(f"{NB_API}/api/graphql").mock(side_effect=_gql_handler())
    g = make_guardian(**NB_KW)
    await g.tick()
    assert gql.call_count == 0  # no incident => no polling
    prom_state.p95_s = 1.9
    await ticks(g, 3)
    le = g.snapshot()["last_execution"]
    assert le["execution_id"] == "ex-1" and le["status"] == "RUNNING"
    assert le["workflow"] == "sale-incident-guard"
    assert {"id": "t2", "type": "core.approval", "status": "WAITING"} in le["tasks"]


async def test_last_execution_unavailable_stays_null(mocked, prom_state, make_guardian):
    mocked.post(f"{NB_API}/api/graphql").mock(side_effect=_gql_handler(fail_list=True))
    g = make_guardian(**NB_KW)
    prom_state.p95_s = 1.9
    await ticks(g, 4)
    assert g.snapshot()["last_execution"] is None
    assert g.snapshot()["incident"]["status"] == "firing"


# ------------------------------------------------------------- HTTP API
def test_http_api_shape(mocked, prom_state, make_guardian):
    g = make_guardian()
    app = create_app(guardian=g, start_loop=False)
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"ok": True}
        st = c.get("/api/state").json()
        for key in ("armed", "slo", "rps", "replicas", "flags", "incident", "last_execution", "history", "feed"):
            assert key in st
        assert set(st["slo"]["/checkout"]) >= {"p95_ms", "error_rate", "target_p95_ms", "breaching", "breach_count"}
        assert c.post("/api/arm", json={"armed": False}).json() == {"armed": False}
        assert c.get("/api/state").json()["armed"] is False
        assert c.post("/api/feed", json={"source": "ops-bot", "text": "approved by Ravi"}).json() == {"ok": True}
        feed = c.get("/api/state").json()["feed"]
        assert feed[-1]["source"] == "ops-bot" and feed[-1]["text"] == "approved by Ravi" and feed[-1]["ts"]
        prom_state.p95_s, prom_state.rps = 0.95, 298.1
        s = c.post("/api/rehearse/sample", json={"label": "pass1", "target_rps": 300}).json()
        assert s["p95_ms"] == {"/checkout": 950.0} and s["rps"] == 298.1
        assert s["error_rate"] == {"/checkout": 0.0} and s["breaching"] == {"/checkout": True}


def test_history_and_feed_are_bounded(make_guardian):
    g = make_guardian()
    for i in range(80):
        g.add_feed("guardian", f"e{i}")
    for i in range(200):
        g.history.append({"ts": str(i)})
    st = g.snapshot()
    assert len(st["feed"]) == 50 and st["feed"][-1]["text"] == "e79"
    assert len(st["history"]) == 180
