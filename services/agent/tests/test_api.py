import json

from app.models import Action, Option, Plan
from app.store import new_plan_id

from .conftest import (
    CPU_INCIDENT,
    DB_INCIDENT,
    GUARDIAN_STATE,
    TOKEN,
    FakeRds,
    drain,
    make_settings,
)


async def test_healthz_and_token(make_service, api, mocked):
    svc = await make_service()
    c = api(svc)
    assert (await c.get("/healthz")).json() == {"ok": True}
    r = await c.post("/api/incident", json={"incident": CPU_INCIDENT}, headers={"X-Insidious-Token": "wrong"})
    assert r.status_code == 401


async def test_cpu_incident_plan_approval_and_execution(make_service, api, mocked, cap, tmp_path):
    svc = await make_service()
    c = api(svc)
    r = await c.post("/api/incident", json={"source": "guardian", "incident": CPU_INCIDENT, "state": GUARDIAN_STATE})
    assert r.status_code == 202
    plan_id = r.json()["plan_id"]
    assert plan_id.startswith("plan-")

    # one active plan per incident
    r2 = await c.post("/api/incident", json={"incident": CPU_INCIDENT})
    assert r2.status_code == 202 and r2.json()["plan_id"] == plan_id and r2.json()["deduplicated"] is True

    await drain(svc)
    plan = (await c.get(f"/api/plans/{plan_id}")).json()
    assert plan["status"] == "pending_approval" and plan["planner"] == "rules" and plan["incident_kind"] == "cpu"
    assert plan["incident_id"] == CPU_INCIDENT["id"]
    assert [o["key"] for o in plan["options"]] == ["recommended", "alt1", "reject"]
    assert plan["numbers"]["replicas_from"] == 1 and plan["numbers"]["replicas_to"] == 3
    assert plan["approval"] == {"ok": True, "pending_id": "p-1"}

    # approval request to ops-bot
    assert len(cap.approvals) == 1
    ap = cap.approvals[0]
    assert ap["plan_id"] == plan_id and ap["action"] == "agent_plan"
    assert ap["founder_summary"] == plan["founder_summary_en"] and ap["summary_en"] == plan["technical_summary"]
    assert ap["options"] == [{"key": o["key"], "title_en": o["title_en"]} for o in plan["options"]]
    assert cap.approval_headers[0]["x-insidious-token"] == TOKEN
    assert any("plan ready" in f for f in cap.feed)

    listed = (await c.get("/api/plans")).json()
    assert listed[0]["id"] == plan_id

    # founder approves the recommended option
    r = await c.post(f"/api/plans/{plan_id}/decision",
                     json={"option": "recommended", "channel": "whatsapp-text", "actor": "founder"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert [x["type"] for x in body["results"]] == ["flagsmith.set_flag", "k8s.scale"]
    assert all(x["ok"] and not x["dry_run"] for x in body["results"])
    assert body["founder_text_en"].startswith("Done: paused product suggestions")
    assert "3 servers" in body["founder_text_en"]

    # external calls happened, in order, with the contract payloads
    assert cap.fs_patches == [("11", {"enabled": False})]
    path, ctype, payload = cap.k8s_patches[0]
    assert path == "/apis/apps/v1/namespaces/store/deployments/store-api/scale"
    assert ctype == "application/merge-patch+json" and payload == {"spec": {"replicas": 3}}
    assert sum("Agent: " in f for f in cap.feed) >= 3

    stored = (await c.get(f"/api/plans/{plan_id}")).json()
    assert stored["status"] == "done" and stored["decision"]["channel"] == "whatsapp-text"
    assert len(stored["results"]) == 2
    on_disk = json.loads((tmp_path / "data" / "plans" / f"{plan_id}.json").read_text())
    assert on_disk["status"] == "done"

    # a second decision is refused
    r = await c.post(f"/api/plans/{plan_id}/decision", json={"option": "recommended"})
    assert r.status_code == 409


async def test_reject_option(make_service, api, mocked, cap):
    svc = await make_service()
    c = api(svc)
    plan_id = (await c.post("/api/incident", json={"incident": CPU_INCIDENT})).json()["plan_id"]
    await drain(svc)
    r = await c.post(f"/api/plans/{plan_id}/decision", json={"option": "reject", "channel": "whatsapp-voice"})
    assert r.json()["status"] == "rejected" and r.json()["results"] == []
    assert "not change anything" in r.json()["founder_text_en"]
    assert cap.fs_patches == [] and cap.k8s_patches == []
    # a rejected plan frees the incident: a new plan may be made
    r2 = await c.post("/api/incident", json={"incident": CPU_INCIDENT})
    assert r2.json()["plan_id"] != plan_id


def _stored_plan(svc, actions, incident_id="inc-1"):
    plan = Plan(id=new_plan_id(), created_at="2026-11-25T14:00:00Z", incident_id=incident_id,
                status="pending_approval", founder_summary_en="Test", technical_summary="t",
                options=[Option(key="recommended", title_en="Do it", actions=actions),
                         Option(key="reject", title_en="Do nothing for now")])
    return svc.store.save(plan)


async def test_decision_guardrail_violation_executes_nothing(make_service, api, mocked, cap):
    svc = await make_service()
    c = api(svc)
    plan = _stored_plan(svc, [
        Action(type="flagsmith.set_flag", params={"flag": "heavy_features", "enabled": False}),
        Action(type="k8s.scale", params={"namespace": "store", "deployment": "store-api", "replicas": 10}),
    ])
    r = await c.post(f"/api/plans/{plan.id}/decision", json={"option": "recommended"})
    body = r.json()
    assert body["status"] == "failed" and "safety limit" in body["founder_text_en"]
    assert "max_replicas" in body["results"][0]["technical"]
    assert cap.fs_patches == [] and cap.k8s_patches == []
    assert svc.store.get(plan.id).status == "failed"


async def test_decision_unknown_option_and_plan(make_service, api, mocked):
    svc = await make_service()
    c = api(svc)
    plan = _stored_plan(svc, [Action(type="notify.founder", params={"text_en": "hi"})])
    assert (await c.post(f"/api/plans/{plan.id}/decision", json={"option": "alt1"})).status_code == 400
    assert (await c.post("/api/plans/plan-nope/decision", json={"option": "reject"})).status_code == 404
    assert (await c.post(f"/api/plans/{plan.id}/decision", json={"option": "bogus"})).status_code == 422


async def test_decision_failure_keeps_going_and_reports(make_service, api, mocked, cap):
    svc = await make_service()
    c = api(svc)
    plan = _stored_plan(svc, [
        Action(type="flagsmith.set_flag", params={"flag": "missing_flag", "enabled": False}),  # no featurestate
        Action(type="k8s.scale", params={"namespace": "store", "deployment": "store-api", "replicas": 2}),
    ])
    body = (await c.post(f"/api/plans/{plan.id}/decision", json={"option": "recommended"})).json()
    assert body["status"] == "failed"
    assert [r["ok"] for r in body["results"]] == [False, True]
    assert body["results"][0]["detail"] == "Could not turn missing flag off"
    assert body["founder_text_en"].startswith("I did part of it.")
    assert cap.k8s_patches and cap.k8s_patches[0][2] == {"spec": {"replicas": 2}}


async def test_db_incident_with_rds_replica_watcher(make_service, api, mocked, cap, tmp_path):
    s = make_settings(tmp_path, rds_id="insidious-db",
                      database_url="postgresql://jeffy:s3cr%40t@insidious-db.abc.rds.amazonaws.com:5432/jeffy")
    rds = FakeRds(statuses=["creating", "backing-up", "available"])
    svc = await make_service(s, rds=rds)
    c = api(svc)
    state = dict(GUARDIAN_STATE, slo={"db:primary": {"breaching": True}})
    mocked.get("http://guardian.test/api/state").respond(200, json=state)
    plan_id = (await c.post("/api/incident", json={"incident": DB_INCIDENT})).json()["plan_id"]
    await drain(svc)
    plan = (await c.get(f"/api/plans/{plan_id}")).json()
    assert plan["incident_kind"] == "db"
    rec = plan["options"][0]
    assert [a["type"] for a in rec["actions"]] == ["db.set_pool", "aws.rds.create_read_replica"]
    assert plan["options"][1]["actions"][0] == {"type": "aws.rds.modify_instance_class",
                                                 "params": {"db_id": "insidious-db", "instance_class": "db.t4g.small"}}

    body = (await c.post(f"/api/plans/{plan_id}/decision", json={"option": "recommended"})).json()
    assert body["status"] == "done", body
    await drain(svc)  # background RDS watcher

    creates = [kw for name, kw in rds.calls if name == "create_db_instance_read_replica"]
    assert creates == [{"DBInstanceIdentifier": "insidious-db-read-1", "SourceDBInstanceIdentifier": "insidious-db",
                        "DBInstanceClass": "db.t4g.micro"}]
    # db_pool remote config, then (after the replica became available) db_read_url + read_from_replica
    assert cap.fs_patches[0] == ("13", {"feature_state_value": '{"pool_size":10,"max_overflow":5,"timeout_s":5.0}'})
    assert cap.fs_patches[1] == ("14", {"feature_state_value": "postgresql://jeffy:s3cr%40t@"
                                        "insidious-db-read-1.abc.ap-south-1.rds.amazonaws.com:5432/jeffy"})
    assert cap.fs_patches[2] == ("12", {"enabled": True})
    assert any("Database copy is ready" in n["text_en"] for n in cap.notifies)
    # the read URL (password) never appears in results or the feed
    assert "s3cr" not in json.dumps(body) and not any("s3cr" in f for f in cap.feed)


async def test_modify_instance_class_watcher(make_service, api, mocked, cap, tmp_path):
    rds = FakeRds()
    svc = await make_service(make_settings(tmp_path, rds_id="insidious-db"), rds=rds)
    c = api(svc)
    plan = _stored_plan(svc, [Action(type="aws.rds.modify_instance_class",
                                     params={"db_id": "insidious-db", "instance_class": "db.t4g.small"})])
    body = (await c.post(f"/api/plans/{plan.id}/decision", json={"option": "recommended"})).json()
    assert body["status"] == "done"
    await drain(svc)
    assert ("modify_db_instance", {"DBInstanceIdentifier": "insidious-db", "DBInstanceClass": "db.t4g.small",
                                   "ApplyImmediately": True}) in rds.calls
    assert any("bigger machine" in n["text_en"] for n in cap.notifies)


async def test_dry_run_mode_changes_nothing(make_service, api, mocked, cap, tmp_path):
    rds = FakeRds()
    svc = await make_service(make_settings(tmp_path, dry_run=True, rds_id="insidious-db"), rds=rds)
    c = api(svc)
    plan = _stored_plan(svc, [
        Action(type="flagsmith.set_flag", params={"flag": "heavy_features", "enabled": False}),
        Action(type="k8s.scale", params={"namespace": "store", "deployment": "store-api", "replicas": 3}),
        Action(type="aws.rds.create_read_replica", params={"source_db_id": "insidious-db",
                                                           "replica_id": "insidious-db-read-1",
                                                           "instance_class": "db.t4g.micro"}),
        Action(type="notify.founder", params={"text_en": "hello"}),
    ])
    body = (await c.post(f"/api/plans/{plan.id}/decision", json={"option": "recommended"})).json()
    assert body["status"] == "done" and all(r["dry_run"] for r in body["results"])
    assert "practice run" in body["results"][0]["detail"]
    assert body["founder_text_en"].count("practice run") == 1 and "(practice run" not in body["founder_text_en"]
    assert cap.fs_patches == [] and cap.k8s_patches == [] and cap.notifies == []
    assert [c for c in rds.calls if c[0] != "describe_db_instances"] == []


async def test_ai_agent_off_only_notifies(make_service, api, mocked, cap):
    svc = await make_service(runtime_kw={"ai_agent": False})
    c = api(svc)
    r = await c.post("/api/incident", json={"incident": CPU_INCIDENT})
    assert r.status_code == 202 and r.json() == {"plan_id": None, "mode": "notify_only"}
    await drain(svc)
    assert svc.store.all() == [] and cap.approvals == []
    assert len(cap.notifies) == 1 and "switched off" in cap.notifies[0]["text_en"]


async def test_resolved_incident_is_ignored(make_service, api, mocked, cap):
    svc = await make_service()
    c = api(svc)
    r = await c.post("/api/incident", json={"incident": dict(CPU_INCIDENT, status="resolved")})
    assert r.status_code == 202 and r.json()["status"] == "ignored"
    await drain(svc)
    assert svc.store.all() == []


async def test_autopilot_executes_recommended(make_service, api, mocked, cap):
    svc = await make_service(runtime_kw={"autopilot": True})
    c = api(svc)
    plan_id = (await c.post("/api/incident", json={"incident": CPU_INCIDENT})).json()["plan_id"]
    await drain(svc)
    assert svc.store.get(plan_id).status == "done" and cap.approvals == []
    assert cap.notifies and cap.notifies[0]["text_en"].startswith("Autopilot: Done:")


async def test_ask_mock(make_service, api, mocked):
    svc = await make_service()
    c = api(svc)
    r = await c.post("/api/ask", json={"question": "dukaan kaisi chal rahi hai?", "language": "hi-IN"})
    assert r.status_code == 200
    ans = r.json()["answer_en"]
    assert "Checkout is slow" in ans and "1.8 seconds" in ans


async def test_plans_survive_restart(make_service, tmp_path, mocked):
    s = make_settings(tmp_path)
    svc = await make_service(s)
    plan = _stored_plan(svc, [Action(type="notify.founder", params={"text_en": "hi"})])
    svc2 = await make_service(s)
    assert svc2.store.get(plan.id).status == "pending_approval"


async def test_legacy_festivekit_token_header_accepted(make_service, api, mocked):
    svc = await make_service()
    c = api(svc)
    c.headers.pop("X-Insidious-Token", None)  # the fixture client sends the new header by default
    r = await c.post("/api/incident", json={"incident": CPU_INCIDENT}, headers={"X-FestiveKit-Token": "wrong"})
    assert r.status_code == 401
    r = await c.post("/api/incident", json={"source": "guardian", "incident": CPU_INCIDENT, "state": GUARDIAN_STATE},
                     headers={"X-FestiveKit-Token": svc.s.internal_token})
    assert r.status_code == 202
