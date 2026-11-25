import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from insidious import Insidious, FlagClient, load_config, load_flags, pick_variant
from insidious.degrade import degradable, set_context

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def flags_doc():
    return load_flags(REPO / "flagsmith" / "flags.yaml")


@pytest.fixture()
def client(flags_doc):
    return FlagClient(flags_doc)


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ----------------------------------------------------------------------------- config
def test_config_loads_repo_files(monkeypatch):
    monkeypatch.setenv("INSIDIOUS_CONFIG", str(REPO / "insidious.yaml"))
    monkeypatch.setenv("INSIDIOUS_FLAGS", str(REPO / "flagsmith" / "flags.yaml"))
    cfg = load_config()
    assert cfg["app"]["name"] == "jeffy"
    assert cfg["degradable"]["parent_flag"] == "heavy_features"
    names = [f["name"] for f in load_flags()["features"]]
    assert "checkout_flow" in names and "heavy_features" in names


def test_config_falls_back_to_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("INSIDIOUS_FLAGS", str(tmp_path / "missing.yaml"))
    doc = load_flags(tmp_path / "missing.yaml")
    assert any(f["name"] == "heavy_features" for f in doc["features"])


# ----------------------------------------------------------------------------- offline evaluation
def test_offline_defaults(client):
    assert client.source == "offline"
    assert client.is_enabled("heavy_features")
    assert client.is_enabled("recommendations")
    assert not client.is_enabled("festive_collection")
    assert client.value("payment_gateway") == "razorpay"
    states = client.env_states()
    assert states == {
        "heavy_features": True,
        "recommendations": True,
        "hd_images": True,
        "live_reviews": True,
        "checkout_flow": "full",
        "payment_gateway": "razorpay",
        "festive_collection": False,
        "source": "offline",
    }


def test_parent_dependency_enforced(client):
    client.set_dev_overrides({"heavy_features": False})
    for child in ("recommendations", "hd_images", "live_reviews"):
        assert not client.is_enabled(child)
        assert not client.is_enabled(child, identity="sid-1", traits={"cart_value": 100})
    # a child forced on still stays off while the parent is off
    client.set_dev_overrides({"recommendations": True})
    assert not client.is_enabled("recommendations")
    # unrelated flags untouched
    assert client.value("payment_gateway") == "razorpay"
    client.set_dev_overrides({"heavy_features": None})
    assert client.is_enabled("recommendations")


def test_child_can_be_shed_alone(client):
    client.set_dev_overrides({"recommendations": False})
    assert client.is_enabled("heavy_features")
    assert not client.is_enabled("recommendations")
    assert client.is_enabled("hd_images")


def test_multivariate_deterministic_and_split(client):
    v1 = client.variant("checkout_flow", "sid-abc")
    assert v1 == client.variant("checkout_flow", "sid-abc")
    assert v1 in {"full", "lite"}
    seen = [client.variant("checkout_flow", f"sid-{i}") for i in range(2000)]
    lite = seen.count("lite") / len(seen)
    assert 0.4 < lite < 0.6
    assert pick_variant("checkout_flow", "x", [{"value": "a", "percentage": 100}], "d") == "a"
    assert pick_variant("checkout_flow", "x", [], "d") == "d"
    # environment level (no identity) -> default value
    assert client.variant("checkout_flow", None) == "full"


def test_dev_overrides_values(client):
    client.set_dev_overrides({"payment_gateway": "cashfree", "checkout_flow": "lite"})
    assert client.value("payment_gateway") == "cashfree"
    assert client.variant("checkout_flow", "anyone") == "lite"
    assert client.env_states()["payment_gateway"] == "cashfree"
    client.set_dev_overrides({}, replace=True)
    assert client.dev_overrides == {}
    assert client.value("payment_gateway") == "razorpay"


def test_offline_segments(flags_doc):
    doc = {
        "features": [
            {"name": "heavy_features", "type": "FLAG", "default_enabled": False},
            {"name": "recommendations", "type": "FLAG", "default_enabled": True, "depends_on": "heavy_features"},
        ],
        "segments": flags_doc["segments"],
    }
    c = FlagClient(doc)
    assert not c.is_enabled("recommendations", "sid-1", {"cart_value": 500})
    assert c.is_enabled("recommendations", "sid-1", {"cart_value": 2500})  # high_value_cart override
    assert not c.is_enabled("recommendations")  # environment level ignores segments


# ----------------------------------------------------------------------------- flagsmith path (fake client)
class _Flag:
    def __init__(self, enabled, value=None):
        self.enabled, self.value = enabled, value


class _Flags:
    def __init__(self, d):
        self.d = d

    def get_flag(self, name):
        return self.d.get(name, _Flag(False, None))


class FakeFlagsmith:
    def __init__(self):
        self.fail = False
        self._local_evaluation = object()
        self.env = {
            "heavy_features": _Flag(True),
            "recommendations": _Flag(True),
            "hd_images": _Flag(True),
            "live_reviews": _Flag(True),
            "checkout_flow": _Flag(True, "full"),
            "payment_gateway": _Flag(True, "razorpay"),
            "festive_collection": _Flag(False),
        }
        self.identity_calls = []

    def get_environment_flags(self):
        if self.fail:
            raise ConnectionError("flagsmith down")
        return _Flags(self.env)

    def get_identity_flags(self, identifier, traits=None):
        if self.fail:
            raise ConnectionError("flagsmith down")
        self.identity_calls.append((identifier, traits))
        d = dict(self.env)
        if (traits or {}).get("cart_value", 0) > 2000:
            d["heavy_features"] = _Flag(True)
        d["checkout_flow"] = _Flag(True, "lite")
        return _Flags(d)


def test_flagsmith_path_and_parent_enforcement(flags_doc):
    fake = FakeFlagsmith()
    c = FlagClient(flags_doc, client=fake)
    assert c.source == "flagsmith"
    assert c.env_states()["source"] == "flagsmith"
    fake.env["heavy_features"] = _Flag(False)  # NudgeBee flipped the parent; children still "on" in Flagsmith
    assert not c.is_enabled("recommendations")
    assert not c.is_enabled("recommendations", "sid", {"cart_value": 100})
    assert c.is_enabled("recommendations", "sid", {"cart_value": 3000})  # segment override keeps big carts
    assert fake.identity_calls[-1] == ("sid", {"cart_value": 3000})
    assert c.variant("checkout_flow", "sid") == "lite"


def test_flagsmith_errors_fall_back(flags_doc):
    fake = FakeFlagsmith()
    c = FlagClient(flags_doc, client=fake)
    fake.env["payment_gateway"] = _Flag(True, "cashfree")
    assert c.env_states()["payment_gateway"] == "cashfree"  # primes last-known
    fake.fail = True
    assert c.env_states()["payment_gateway"] == "cashfree"  # last known, no exception
    assert c.is_enabled("recommendations", "sid", {"cart_value": 10})
    assert c.errors >= 1


def test_flagsmith_errors_without_last_known_use_defaults(flags_doc):
    fake = FakeFlagsmith()
    fake.fail = True
    c = FlagClient(flags_doc, client=fake)
    assert c.value("payment_gateway") == "razorpay"


def test_not_ready_document_never_calls_api(flags_doc):
    fake = FakeFlagsmith()
    fake._local_evaluation = None
    c = FlagClient(flags_doc, client=fake)
    assert c.is_enabled("heavy_features")
    assert fake.identity_calls == []


def test_factory_failure_goes_offline(flags_doc):
    def boom(**kwargs):
        raise ValueError("bad key")

    c = FlagClient(flags_doc, server_key="ser.bad", client_factory=boom)
    assert c.source == "offline"
    assert c.is_enabled("heavy_features")


def test_factory_receives_local_eval_kwargs(flags_doc):
    seen = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeFlagsmith()

    FlagClient(flags_doc, server_key="ser.abc", api_url="http://x/api/v1/", refresh_seconds=5, client_factory=factory)
    assert seen["enable_local_evaluation"] is True
    assert seen["environment_refresh_interval_seconds"] == 5
    assert seen["environment_key"] == "ser.abc"
    assert seen["api_url"] == "http://x/api/v1/"
    assert callable(seen["default_flag_handler"])


# ----------------------------------------------------------------------------- degradable
def test_degradable_sync(client):
    calls = []

    @degradable("recommendations", fallback=lambda pid: ["cached"], flags=client)
    def recs(pid):
        calls.append(pid)
        return ["fresh", pid]

    before = sample("insidious_feature_shed_total", feature="recommendations")
    assert recs(1) == ["fresh", 1]
    client.set_dev_overrides({"heavy_features": False})
    assert recs(2) == ["cached"]
    assert calls == [1]
    assert sample("insidious_feature_shed_total", feature="recommendations") == before + 1


def test_degradable_async_and_value_fallback(client):
    @degradable("live_reviews", fallback="cached", flags=client)
    async def reviews():
        return "live"

    async def fb():
        return "async-cached"

    @degradable("live_reviews", fallback=fb, flags=client)
    async def reviews2():
        return "live"

    assert asyncio.run(reviews()) == "live"
    client.set_dev_overrides({"live_reviews": False})
    assert asyncio.run(reviews()) == "cached"
    assert asyncio.run(reviews2()) == "async-cached"


def test_degradable_uses_identity_context(flags_doc):
    doc = {
        "features": [
            {"name": "heavy_features", "type": "FLAG", "default_enabled": False},
            {"name": "recommendations", "type": "FLAG", "default_enabled": True, "depends_on": "heavy_features"},
        ],
        "segments": flags_doc["segments"],
    }
    c = FlagClient(doc)

    @degradable("recommendations", fallback="fallback", flags=c)
    def recs():
        return "full"

    set_context("sid-x", {"cart_value": 100})
    assert recs() == "fallback"
    set_context("sid-x", {"cart_value": 5000})
    assert recs() == "full"
    assert recs(fk_identity="sid-y", fk_traits={"cart_value": 10}) == "fallback"
    set_context(None, None)


def test_degradable_falls_back_on_error(client):
    @degradable("hd_images", fallback="low-res", flags=client)
    def render():
        raise RuntimeError("boom")

    assert render() == "low-res"


# ----------------------------------------------------------------------------- metrics + facade
def _app(fk):
    app = FastAPI()
    fk.install(app)

    @app.get("/product/{id}")
    def product(id: int):
        return {"id": id}

    @app.get("/boom")
    def boom():
        from fastapi import HTTPException

        raise HTTPException(status_code=503)

    return app


def test_metrics_route_template_labels(monkeypatch):
    monkeypatch.delenv("FLAGSMITH_SERVER_KEY", raising=False)
    fk = Insidious.from_env(flags_path=REPO / "flagsmith" / "flags.yaml", config_path=REPO / "insidious.yaml")
    tc = TestClient(_app(fk))
    before = sample("http_requests_total", route="/product/{id}", method="GET", status="200")
    for i in range(3):
        assert tc.get(f"/product/{i}").status_code == 200
    assert sample("http_requests_total", route="/product/{id}", method="GET", status="200") == before + 3
    assert sample("http_requests_total", route="/product/1", method="GET", status="200") == 0
    assert sample("http_request_duration_seconds_count", route="/product/{id}", method="GET", status="200") >= 3
    tc.get("/boom")
    assert sample("http_requests_total", route="/boom", method="GET", status="503") >= 1
    tc.get("/nope/123")
    assert sample("http_requests_total", route="unmatched", method="GET", status="404") >= 1
    body = tc.get("/metrics").text
    assert 'http_request_duration_seconds_bucket{le="0.8",method="GET",route="/product/{id}",status="200"}' in body
    assert 'insidious_flag_state{flag="heavy_features"} 1.0' in body
    assert sample("http_requests_total", route="/metrics", method="GET", status="200") == 0  # excluded


def test_facade_track_and_orders_counter(monkeypatch):
    monkeypatch.delenv("FLAGSMITH_SERVER_KEY", raising=False)
    fk = Insidious.from_env(flags_path=REPO / "flagsmith" / "flags.yaml")
    before = sample("insidious_orders_total", checkout_flow="lite", gateway="cashfree")
    fk.track("checkout_started", identity="s1", properties={"checkout_flow": "lite"})
    fk.track("order_placed", identity="s1", properties={"checkout_flow": "lite", "gateway": "cashfree"})
    assert sample("insidious_orders_total", checkout_flow="lite", gateway="cashfree") == before + 1
    assert fk.events.recent(1)[0]["event"] == "order_placed"
    summary = fk.events.experiment_summary()
    assert summary["lite"]["exposures"] == 1 and summary["lite"]["conversions"] == 1


def test_facade_dev_mode_gate(monkeypatch):
    monkeypatch.delenv("FLAGSMITH_SERVER_KEY", raising=False)
    monkeypatch.setenv("INSIDIOUS_DEV", "0")
    fk = Insidious.from_env(flags_path=REPO / "flagsmith" / "flags.yaml")
    with pytest.raises(PermissionError):
        fk.set_dev_overrides({"heavy_features": False})
    monkeypatch.setenv("INSIDIOUS_DEV", "1")
    fk = Insidious.from_env(flags_path=REPO / "flagsmith" / "flags.yaml")
    fk.set_dev_overrides({"heavy_features": False})
    assert fk.env_states()["recommendations"] is False

    @fk.degradable("recommendations", fallback="cached")
    def recs():
        return "fresh"

    assert recs() == "cached"


def test_placeholder_key_stays_offline(monkeypatch):
    monkeypatch.setenv("FLAGSMITH_SERVER_KEY", "ser.xxxxxxxxxxxxxxxxxxxx")
    fk = Insidious.from_env(flags_path=REPO / "flagsmith" / "flags.yaml")
    assert fk.flags.source == "offline"


def test_experimentation_hook_forwards_when_enabled(monkeypatch):
    from insidious.events import EventTracker

    sent = []

    class FakeClient:
        _event_processor = object()

        def track_event(self, event, identifier=None, metadata=None):
            sent.append((event, identifier, metadata))

    monkeypatch.setenv("INSIDIOUS_FLAGSMITH_EVENTS", "1")
    t = EventTracker(flagsmith_client=lambda: FakeClient())
    t.track("order_placed", "sid-9", {"checkout_flow": "full", "gateway": "razorpay"})
    assert sent == [("order_placed", "sid-9", {"checkout_flow": "full", "gateway": "razorpay"})]
    monkeypatch.setenv("INSIDIOUS_FLAGSMITH_EVENTS", "0")
    t2 = EventTracker(flagsmith_client=lambda: FakeClient())
    t2.track("order_placed", "sid-9", {})
    assert len(sent) == 1


def test_legacy_festivekit_env_and_config_fallback(monkeypatch, tmp_path):
    from insidious import config as cfg

    monkeypatch.delenv("INSIDIOUS_DEV", raising=False)
    monkeypatch.setenv("FESTIVEKIT_DEV", "1")
    assert cfg.env("INSIDIOUS_DEV") == "1"
    monkeypatch.setenv("INSIDIOUS_DEV", "0")
    assert cfg.env("INSIDIOUS_DEV") == "0"  # new name wins
    legacy = tmp_path / "festivekit.yaml"
    legacy.write_text("app: {name: legacy}\n")
    monkeypatch.delenv("INSIDIOUS_CONFIG", raising=False)
    monkeypatch.delenv("FESTIVEKIT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cfg.find_config_path() == legacy.resolve()
    (tmp_path / "insidious.yaml").write_text("app: {name: new}\n")
    assert cfg.find_config_path() == (tmp_path / "insidious.yaml").resolve()
