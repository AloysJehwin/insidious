"""Shared fixtures. Every external system is mocked: respx for HTTP, fakes for boto3 / Anthropic / Flagsmith SDK."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from app.config import DEFAULT_GUARDRAILS, Settings
from app.flags import AgentRuntime, RuntimeConfig
from app.k8s import K8sClient
from app.main import create_app
from app.service import AgentService

GUARDIAN = "http://guardian.test"
OPS = "http://ops.test"
PROM = "http://prom.test"
FS = "http://fs.test/api/v1"
K8S = "https://k8s.test"
NB = "http://nb.test"
TOKEN = "test-token"
ENV_KEY = "envkey123"

GUARDIAN_STATE = {
    "armed": True,
    "slo": {"/checkout": {"p95_ms": 1840.0, "error_rate": 0.0, "target_p95_ms": 800, "breaching": True,
                          "breach_count": 3}},
    "rps": 55.0,
    "replicas": {"store-api": 1},
    "flags": {"heavy_features": True, "recommendations": True, "payment_gateway": "razorpay"},
    "incident": {"id": "slo-checkout-p95-1732545600", "status": "firing", "since": "2026-11-25T14:02:05Z"},
    "history": [{"ts": "2026-11-25T14:02:00Z", "p95_ms": {"/checkout": 1840.0}, "rps": 55.0}] * 20,
    "feed": [],
}

CPU_INCIDENT = {
    "id": "slo-checkout-p95-1732545600", "status": "firing", "rule_name": "insidious-slo",
    "title": "Checkout p95 1840ms breaches 800ms SLO at 55 rps",
    "labels": {"route": "/checkout", "p95_ms": "1840", "rps": "55", "heavy_features": "on"},
}
DB_INCIDENT = {
    "id": "slo-db-primary-1732545700", "status": "firing", "rule_name": "insidious-db",
    "title": "DB pool primary 100% in use", "labels": {"route": "db:primary"},
}

DEPLOYMENT = {
    "metadata": {"name": "store-api"},
    "spec": {"replicas": 1, "template": {"spec": {"containers": [
        {"name": "store-api", "resources": {"limits": {"cpu": "500m", "memory": "512Mi"},
                                             "requests": {"cpu": "250m", "memory": "128Mi"}}}]}}},
    "status": {"readyReplicas": 1, "availableReplicas": 1},
}
POD = {
    "metadata": {"name": "store-api-abc", "labels": {"app": "store-api"}},
    "spec": {"containers": [{"name": "store-api"}]},
    "status": {"phase": "Running", "containerStatuses": [
        {"name": "store-api", "ready": True, "restartCount": 2,
         "lastState": {"terminated": {"reason": "OOMKilled"}}}]},
}
POD_METRICS = {"items": [{"metadata": {"name": "store-api-abc"},
                          "containers": [{"name": "store-api", "usage": {"cpu": "490000000n", "memory": "300Mi"}}]}]}
POD_LOG = "\n".join(["INFO GET /checkout 200"] * 5 + ["ERROR checkout timed out", "Traceback (most recent call last):",
                                                      "  File x", "ValueError: boom", "INFO ok"])


class FakeRuntime(RuntimeConfig):
    def __init__(self, settings: Settings, **kw: Any):
        super().__init__(settings)
        self.rt = AgentRuntime(provider="anthropic", model="claude-sonnet-5-5", source="test", **kw)

    async def get(self) -> AgentRuntime:
        return self.rt


class FakeRds:
    def __init__(self, cls: str = "db.t4g.micro", replicas: list | None = None, statuses: list[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.cls = cls
        self.replicas = replicas or []
        self.statuses = list(statuses or ["creating", "available"])

    def create_db_instance_read_replica(self, **kw):
        self.calls.append(("create_db_instance_read_replica", kw))
        return {"DBInstance": {"DBInstanceIdentifier": kw["DBInstanceIdentifier"], "DBInstanceStatus": "creating"}}

    def modify_db_instance(self, **kw):
        self.calls.append(("modify_db_instance", kw))
        self.cls = kw["DBInstanceClass"]
        return {}

    def describe_db_instances(self, **kw):
        self.calls.append(("describe_db_instances", kw))
        db_id = kw["DBInstanceIdentifier"]
        status = self.statuses.pop(0) if self.statuses and db_id.endswith("-read-1") else "available"
        return {"DBInstances": [{
            "DBInstanceIdentifier": db_id, "DBInstanceStatus": status, "DBInstanceClass": self.cls,
            "Engine": "postgres", "Endpoint": {"Address": f"{db_id}.abc.ap-south-1.rds.amazonaws.com", "Port": 5432},
            "ReadReplicaDBInstanceIdentifiers": self.replicas}]}


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        guardian_url=GUARDIAN, ops_bot_url=OPS, prometheus_url=PROM, internal_token=TOKEN,
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports", mock=True, dry_run=False,
        flagsmith_admin_url=FS, flagsmith_org_api_key="orgkey", flagsmith_environment_key=ENV_KEY,
        nudgebee_url=NB, nudgebee_token="", nudgebee_account_id="", nudgebee_dry_run=True,
        rds_poll_seconds=0.01, rds_watch_seconds=5, database_url="",
        guardrails=dict(DEFAULT_GUARDRAILS),
    )
    base.update(kw)
    return Settings(**base)


def prom_empty(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})


class Captured:
    def __init__(self):
        self.approvals: list[dict] = []
        self.approval_headers: list[dict] = []
        self.notifies: list[dict] = []
        self.feed: list[str] = []
        self.fs_patches: list[tuple[str, dict]] = []
        self.k8s_patches: list[tuple[str, str, dict]] = []


@pytest.fixture
def cap() -> Captured:
    return Captured()


@pytest.fixture
def mocked(cap: Captured):
    """respx router with healthy defaults for every external HTTP dependency."""
    fs_ids = {"heavy_features": 11, "read_from_replica": 12, "db_pool": 13, "db_read_url": 14, "payment_gateway": 15}

    def fs_list(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("feature_name")
        rows = [
            {"id": 900, "feature": 1, "identity": 7, "feature_segment": None, "enabled": True},
            {"id": 901, "feature": 1, "identity": None, "feature_segment": 3, "enabled": True},
        ]
        if name in fs_ids:
            rows.append({"id": fs_ids[name], "feature": 1, "identity": None, "feature_segment": None, "enabled": True})
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})

    def fs_patch(request: httpx.Request) -> httpx.Response:
        cap.fs_patches.append((request.url.path.rstrip("/").split("/")[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    def k8s_patch(request: httpx.Request) -> httpx.Response:
        cap.k8s_patches.append((request.url.path, request.headers.get("content-type"), json.loads(request.content)))
        return httpx.Response(200, json={"spec": {"replicas": 3}})

    def approval(request: httpx.Request) -> httpx.Response:
        cap.approvals.append(json.loads(request.content))
        cap.approval_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True, "pending_id": "p-1"})

    def notify(request: httpx.Request) -> httpx.Response:
        cap.notifies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def feed(request: httpx.Request) -> httpx.Response:
        cap.feed.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GUARDIAN}/api/state").respond(200, json=GUARDIAN_STATE)
        router.post(f"{GUARDIAN}/api/feed").mock(side_effect=feed)
        router.post(f"{OPS}/nb/approval-request").mock(side_effect=approval)
        router.post(f"{OPS}/nb/notify").mock(side_effect=notify)
        router.get(f"{PROM}/api/v1/query").mock(side_effect=prom_empty)
        router.get(url__regex=rf"^{FS}/environments/{ENV_KEY}/featurestates/.*").mock(side_effect=fs_list)
        router.patch(url__regex=rf"^{FS}/environments/{ENV_KEY}/featurestates/\d+/$").mock(side_effect=fs_patch)
        router.get(f"{K8S}/apis/apps/v1/namespaces/store/deployments").respond(200, json={"items": [DEPLOYMENT]})
        router.get(f"{K8S}/api/v1/namespaces/store/pods").respond(200, json={"items": [POD]})
        router.get(f"{K8S}/apis/metrics.k8s.io/v1beta1/namespaces/store/pods").respond(200, json=POD_METRICS)
        router.get(url__regex=rf"^{K8S}/api/v1/namespaces/store/pods/[^/]+/log").respond(200, text=POD_LOG)
        router.patch(url__regex=rf"^{K8S}/apis/apps/v1/namespaces/store/deployments/.*").mock(side_effect=k8s_patch)
        yield router


async def drain(svc: AgentService) -> None:
    for _ in range(50):
        pending = list(svc.tasks) + list(svc.executor.tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
async def make_service(tmp_path: Path):
    made: list[tuple[AgentService, httpx.AsyncClient]] = []

    async def factory(settings: Settings | None = None, *, runtime_kw: dict | None = None, rds: Any = None,
                      claude: Any = None, k8s: bool = True) -> AgentService:
        s = settings or make_settings(tmp_path)
        client = httpx.AsyncClient(timeout=5)
        kc = K8sClient(enabled=k8s, base_url=K8S, token="sa-token")
        svc = AgentService(s, client, k8s=kc, rds_factory=(lambda: rds) if rds is not None else None,
                           runtime=FakeRuntime(s, **(runtime_kw or {})), claude_client=claude)
        made.append((svc, client))
        return svc

    yield factory
    for svc, client in made:
        await svc.aclose()
        await client.aclose()


@pytest.fixture
def api():
    clients: list[httpx.AsyncClient] = []

    def factory(svc: AgentService) -> httpx.AsyncClient:
        app = create_app(service=svc)
        app.state.svc = svc  # ASGITransport does not run the lifespan
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent.test",
                              headers={"X-Insidious-Token": TOKEN})
        clients.append(c)
        return c

    yield factory


def tool_use_response(plan_input: Any, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None, _request_id="req_test", content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="tool_use", id="toolu_1", name="submit_plan", input=plan_input),
    ])


def text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text=text)])


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records kwargs, returns queued responses or raises queued errors."""

    def __init__(self, *responses: Any):
        self.queue = list(responses)
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            async def create(self, **kw):
                outer.calls.append({"beta": False, **kw})
                return outer._next()

        class _BetaMessages:
            async def create(self, **kw):
                outer.calls.append({"beta": True, **kw})
                return outer._next()

        self.messages = _Messages()
        self.beta = SimpleNamespace(messages=_BetaMessages())

    def _next(self) -> Any:
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
