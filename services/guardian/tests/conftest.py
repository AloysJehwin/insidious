import json
from pathlib import Path

import httpx
import pytest
import respx

from app.config import SLO, Settings
from app.engine import Guardian

PROM = "http://prom.test"
STORE = "http://store.test"
WEBHOOK = "http://nb.test/api/webhooks/event?token=abc123"
NB_API = "http://nbapi.test"
AGENT = "http://agent.test:8004"
T0 = 1732545600.0


class Clock:
    def __init__(self, t: float = T0, step: float = 5.0):
        self.t = t - step
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class PromState:
    """Mutable values the mocked Prometheus serves. None => empty result. Exception => HTTP failure."""

    def __init__(self):
        self.p95_s = 0.2
        self.err = 0.0
        self.rps = 100.0
        self.replicas = 1
        self.down = False
        # DB pool metrics (insidious_db_*). None = no series (store without a DB / not scraped).
        self.db_in_use = None
        self.db_size = None
        self.db_wait_s = None
        self.replica_in_use = None
        self.replica_size = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        q = request.url.params["query"]
        replica = 'db="replica"' in q
        if "insidious_db_pool_wait_seconds" in q:
            v = None if replica else self.db_wait_s
        elif "insidious_db_pool_in_use" in q:
            v = self.replica_in_use if replica else self.db_in_use
        elif "insidious_db_pool_size" in q:
            v = self.replica_size if replica else self.db_size
        elif q.startswith("histogram_quantile"):
            v = self.p95_s
        elif 'status=~"5.."' in q:
            v = self.err
        elif q.startswith("count(up"):
            v = self.replicas
        else:
            v = self.rps
        result = [] if v is None else [{"metric": {}, "value": [T0, str(v)]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        slos=[SLO("/checkout", 800, 0.02)],
        prometheus_url=PROM,
        store_url=STORE,
        reports_dir=tmp_path,
        nudgebee_event_webhook_url=WEBHOOK,
        nudgebee_dry_run=False,
        k8s_enabled=False,
        api_alias=False,
        armed=True,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def prom_state():
    return PromState()


@pytest.fixture
def mocked(prom_state):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PROM}/api/v1/query").mock(side_effect=prom_state.handler)
        router.get(f"{STORE}/api/flags").mock(return_value=httpx.Response(200, json={
            "heavy_features": True, "recommendations": True, "payment_gateway": "razorpay", "source": "offline"}))
        router.webhook = router.post(url__startswith="http://nb.test/api/webhooks/event").mock(
            return_value=httpx.Response(200, json={"ok": True}))
        router.agent = router.post(f"{AGENT}/api/incident").mock(
            return_value=httpx.Response(202, json={"plan_id": "plan-20261125-140205-ab12"}))
        yield router


@pytest.fixture
async def client():
    async with httpx.AsyncClient(timeout=2) as c:
        yield c


@pytest.fixture
def make_guardian(tmp_path, client):
    def _make(**kw) -> Guardian:
        return Guardian(make_settings(tmp_path, **kw), client, clock=Clock())
    return _make


def webhook_payloads(router) -> list[dict]:
    return [json.loads(call.request.content) for call in router.webhook.calls]
