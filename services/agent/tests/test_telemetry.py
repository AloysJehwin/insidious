import json

import httpx
import pytest

from app.telemetry import classify, extract_facts

from .conftest import CPU_INCIDENT, DB_INCIDENT, PROM


async def test_collect_kubernetes_and_facts(make_service, mocked, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "readiness-20261125-1300.json").write_text(json.dumps({"capacity_gain_if_shed": "+10%"}))
    (reports / "readiness-20261125-1340.json").write_text(json.dumps(
        {"generated_at": "x", "pass1": {"breaking_rps": 420}, "pass2": {"breaking_rps": 680},
         "capacity_gain_if_shed": "+62%"}))
    svc = await make_service()
    tel = await svc.telemetry.collect(CPU_INCIDENT, None)
    k = tel["kubernetes"]
    assert k["available"] and k["deployments"][0]["replicas"] == 1
    assert k["pods"][0]["last_termination_reason"] == "OOMKilled" and k["pods"][0]["restarts"] == 2
    assert k["pod_metrics"]["store-api-abc"] == {"cpu_m": 490, "memory_mi": 300.0, "cpu_pct_of_limit": 98.0}
    logs = k["error_logs"]
    assert len(logs) == 2 and all(l.startswith("store-api-abc: ") for l in logs)  # ERROR + Traceback lines
    assert tel["readiness"]["capacity_gain_if_shed"] == "+62%"
    assert tel["nudgebee_rca"] == {"skipped": "NudgeBee dry-run or not configured"}
    assert tel["rds"] == {"configured": False}
    assert len(tel["guardian"]["history"]) == 12

    f = extract_facts(tel, CPU_INCIDENT, svc.s)
    assert f["kind"] == "cpu" and f["p95_ms"] == 1840 and f["rps"] == 55 and f["replicas"] == 1
    assert f["cpu_pct_of_limit"] == 98.0 and f["oom_killed"] is True and f["cpu_limit"] == "500m"


async def test_collectors_never_crash(make_service, mocked):
    mocked.get(f"{PROM}/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
    mocked.get("http://guardian.test/api/state").mock(side_effect=httpx.ConnectError("refused"))
    mocked.get("https://k8s.test/apis/metrics.k8s.io/v1beta1/namespaces/store/pods").respond(503, text="no metrics")
    svc = await make_service()
    tel = await svc.telemetry.collect(CPU_INCIDENT, {"rps": 12.0, "replicas": {"store-api": 2}})
    assert "error" in tel["db_pool"]
    assert tel["guardian"]["_source"] == "incident_request" and tel["guardian"]["rps"] == 12.0
    assert "metrics_error" in tel["kubernetes"] and tel["kubernetes"]["deployments"]


async def test_outside_cluster(make_service, mocked):
    svc = await make_service(k8s=False)
    tel = await svc.telemetry.collect({}, None, include_rca=False)
    assert tel["kubernetes"]["available"] is False


async def test_db_pool_metrics(make_service, mocked):
    def prom(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        v = {"sum(insidious_db_pool_size) by (db)": 5, "max(insidious_db_pool_size) by (db)": 5,
             "sum(insidious_db_pool_in_use) by (db)": 5}.get(q, 0.4)
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"db": "primary"}, "value": [0, str(v)]}]}})

    mocked.get(f"{PROM}/api/v1/query").mock(side_effect=prom)
    svc = await make_service()
    tel = await svc.telemetry.collect({}, None, include_rca=False, include_logs=False)
    assert tel["db_pool"]["primary"]["utilisation"] == 1.0 and tel["db_pool"]["primary"]["wait_p95_s"] == 0.4
    f = extract_facts(tel, {}, svc.s)
    assert f["db_pool_size"] == 5 and f["db_utilisation"] == 1.0


@pytest.mark.parametrize("incident,guardian,facts,kind", [
    (DB_INCIDENT, {}, {}, "db"),
    (CPU_INCIDENT, {}, {}, "cpu"),
    ({"id": "x"}, {"slo": {"db:primary": {"breaching": True}, "/checkout": {"breaching": False}}}, {}, "db"),
    ({"id": "x"}, {"slo": {"/checkout": {"breaching": True}}}, {"db_utilisation": 1.0}, "cpu"),
    ({"id": "x"}, {}, {"db_wait_p95_s": 0.5, "cpu_pct_of_limit": 30}, "db"),
])
def test_classify(incident, guardian, facts, kind):
    assert classify(incident, guardian, facts) == kind
