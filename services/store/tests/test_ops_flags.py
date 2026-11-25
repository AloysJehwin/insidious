"""/ops/flags: Flagsmith Admin API list + environment featurestate toggles from the cockpit (all HTTP mocked)."""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import main, ops_flags
from app.main import app, fk

ADMIN = "http://flagsmith.test/api/v1"
ENV = "env-test-key"
FEATURES = f"{ADMIN}/projects/7/features/"
STATES = f"{ADMIN}/environments/{ENV}/featurestates/"
FEED = "http://guardian.test:8002/api/feed"
PASS = "diwali-2026"

FEATURE_ROWS = [
    {"id": 1, "name": "heavy_features", "description": "Parent switch. Off = shed every heavy feature at once."},
    {"id": 2, "name": "recommendations", "description": "Personalised recommendations (CPU-heavy)."},
    {"id": 3, "name": "payment_gateway", "description": "Remote config. razorpay | cashfree."},
    {"id": 4, "name": "db_read_url", "description": ""},
    {"id": 5, "name": "admin_password", "description": "Not a real flag; checks name-based masking."},
    {"id": 6, "name": "festive_collection", "description": "Progressive rollout."},
    {"id": 7, "name": "ai_agent", "description": "AI kill switch."},
    {"id": 8, "name": "webhook_url", "description": ""},
]
STATE_ROWS = [
    {"id": 101, "feature": 1, "enabled": True, "feature_state_value": None, "identity": None, "feature_segment": None},
    {"id": 199, "feature": 1, "enabled": True, "feature_state_value": None, "identity": None, "feature_segment": 9},
    {"id": 102, "feature": 2, "enabled": False, "feature_state_value": None, "identity": None, "feature_segment": None},
    {"id": 103, "feature": 3, "enabled": True, "feature_state_value": "razorpay", "identity": None,
     "feature_segment": None},
    {"id": 104, "feature": 4, "enabled": True,
     "feature_state_value": "postgresql://insidious:s3cretpw@replica.internal:5432/jeffy", "identity": None,
     "feature_segment": None},
    {"id": 105, "feature": 5, "enabled": True, "feature_state_value": "hunter2", "identity": None,
     "feature_segment": None},
    {"id": 106, "feature": 6, "enabled": False, "feature_state_value": None, "identity": None, "feature_segment": None},
    {"id": 107, "feature": 7, "enabled": True, "feature_state_value": None, "identity": None, "feature_segment": None},
    {"id": 108, "feature": 8, "enabled": True, "feature_state_value": "https://hooks.example/abc?token=t0psecret",
     "identity": None, "feature_segment": None},
    {"id": 300, "feature": 2, "enabled": True, "feature_state_value": None, "identity": 55, "feature_segment": None},
]


def states_api(request):
    """Fake featurestates endpoint: honours ?feature_name= like Flagsmith does."""
    name = request.url.params.get("feature_name")
    if not name:
        return httpx.Response(200, json=STATE_ROWS)
    fid = next((f["id"] for f in FEATURE_ROWS if f["name"] == name), None)
    return httpx.Response(200, json=[s for s in STATE_ROWS if s["feature"] == fid])


@pytest.fixture(autouse=True)
def flagsmith_env(monkeypatch):
    monkeypatch.setenv("FLAGSMITH_ADMIN_URL", ADMIN)
    monkeypatch.setenv("FLAGSMITH_ORG_API_KEY", "org-test-key")
    monkeypatch.setenv("FLAGSMITH_ENVIRONMENT_KEY", ENV)
    monkeypatch.setenv("FLAGSMITH_PROJECT_ID", "7")
    monkeypatch.setenv("INSIDIOUS_OPS_PASSCODE", PASS)
    monkeypatch.setenv("FLAGSMITH_REFRESH_SECONDS", "5")
    main._ops_flags_invalidate()
    ops_flags._PROJECTS.clear()
    yield
    main._ops_flags_invalidate()
    ops_flags._PROJECTS.clear()


@pytest.fixture()
def client():
    return TestClient(app)


def _by_name(body):
    return {f["name"]: f for f in body["flags"]}


# ------------------------------------------------------------------------------------------- GET /ops/flags
def test_list_flags_groups_and_masks(client):
    with respx.mock() as mock:
        feats = mock.get(FEATURES).mock(return_value=httpx.Response(200, json={"results": FEATURE_ROWS, "next": None}))
        states = mock.get(STATES).mock(return_value=httpx.Response(200, json=STATE_ROWS))
        r = client.get("/ops/flags")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "flagsmith" and body["writable"] is True and body["refresh_seconds"] == 5
    assert feats.calls.last.request.headers["Authorization"] == "Api-Key org-test-key"
    assert states.calls.last.request.headers["Authorization"] == "Api-Key org-test-key"

    flags = _by_name(body)
    # one row per feature: segment overrides and identity overrides are ignored
    assert len(body["flags"]) == len(FEATURE_ROWS)
    assert flags["heavy_features"]["enabled"] is True and flags["heavy_features"]["is_parent"] is True
    assert flags["recommendations"]["enabled"] is False and flags["recommendations"]["parent"] == "heavy_features"
    assert flags["payment_gateway"]["value"] == "razorpay" and flags["payment_gateway"]["options"] == ["razorpay", "cashfree"]
    assert flags["payment_gateway"]["kind"] == "config" and flags["heavy_features"]["kind"] == "flag"

    groups = {n: f["group"] for n, f in flags.items()}
    assert groups["heavy_features"] == groups["recommendations"] == "Load shedding"
    assert groups["payment_gateway"] == "Checkout"
    assert groups["db_read_url"] == "Database"
    assert groups["ai_agent"] == "AI agent"
    assert groups["festive_collection"] == groups["admin_password"] == "Other"
    order = [f["name"] for f in body["flags"]]
    assert order.index("heavy_features") < order.index("recommendations") < order.index("payment_gateway") < \
        order.index("db_read_url") < order.index("ai_agent") < order.index("festive_collection")

    # secrets never leave the store
    raw = r.text
    for secret in ("s3cretpw", "hunter2", "t0psecret"):
        assert secret not in raw
    assert flags["db_read_url"]["value"] == "postgresql://insidious:***@replica.internal:5432/jeffy"
    assert flags["db_read_url"]["masked"] is True
    assert flags["webhook_url"]["value"] == "https://hooks.example/abc?…"
    assert flags["admin_password"]["value"] == ops_flags.MASK
    assert "options" not in flags["db_read_url"]
    # description falls back to flags.yaml when Flagsmith has none
    assert "read replica" in flags["db_read_url"]["description"]


def test_list_flags_is_cached_for_3s(client):
    with respx.mock() as mock:
        feats = mock.get(FEATURES).mock(return_value=httpx.Response(200, json=FEATURE_ROWS))
        states = mock.get(STATES).mock(return_value=httpx.Response(200, json={"results": STATE_ROWS, "next": None}))
        assert client.get("/ops/flags").status_code == 200
        assert client.get("/ops/flags").status_code == 200
    assert feats.call_count == 1 and states.call_count == 1


def test_list_flags_resolves_project_from_environment(client, monkeypatch):
    monkeypatch.delenv("FLAGSMITH_PROJECT_ID")
    with respx.mock() as mock:
        env = mock.get(f"{ADMIN}/environments/{ENV}/").mock(return_value=httpx.Response(200, json={"project": 7}))
        mock.get(FEATURES).mock(return_value=httpx.Response(200, json=FEATURE_ROWS))
        mock.get(STATES).mock(return_value=httpx.Response(200, json=STATE_ROWS))
        r = client.get("/ops/flags")
    assert r.status_code == 200 and env.call_count == 1
    assert "heavy_features" in _by_name(r.json())


def test_list_flags_flagsmith_down_is_502(client):
    with respx.mock() as mock:
        mock.get(FEATURES).mock(side_effect=httpx.ConnectError("down"))
        mock.get(STATES).mock(return_value=httpx.Response(200, json=STATE_ROWS))
        r = client.get("/ops/flags")
    assert r.status_code == 502 and r.json()["error"] == "flagsmith_unavailable"
    assert "org-test-key" not in r.text


def test_list_flags_without_admin_key_falls_back_to_sdk(client, monkeypatch):
    monkeypatch.delenv("FLAGSMITH_ORG_API_KEY")
    with respx.mock() as mock:  # no Flagsmith call at all
        r = client.get("/ops/flags")
        assert not mock.calls
    body = r.json()
    assert r.status_code == 200 and body["source"] == "sdk" and body["writable"] is False
    flags = _by_name(body)
    assert set(fk.flags.features) == set(flags)
    assert flags["heavy_features"]["group"] == "Load shedding" and flags["heavy_features"]["enabled"] is True
    assert flags["payment_gateway"]["value"] == "razorpay" and flags["ops_voice"]["group"] == "Founder voice"


# ------------------------------------------------------------------------------------------- POST /ops/flags/{name}
@pytest.mark.parametrize("headers", [{}, {"X-Ops-Passcode": "wrong"}, {"X-Ops-Passcode": ""}])
def test_set_flag_rejects_missing_or_wrong_passcode(client, headers):
    with respx.mock(assert_all_called=False) as mock:
        lookup = mock.get(STATES).mock(return_value=httpx.Response(200, json=STATE_ROWS))
        patch = mock.patch(url__regex=rf"{STATES}\d+/").mock(return_value=httpx.Response(200, json={}))
        feed = mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "bad_passcode"
    assert not lookup.called and not patch.called and not feed.called


def test_set_flag_503_when_passcode_or_org_key_missing(client, monkeypatch):
    monkeypatch.delenv("INSIDIOUS_OPS_PASSCODE")
    monkeypatch.delenv("FESTIVEKIT_OPS_PASSCODE", raising=False)
    r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 503 and r.json()["error"] == "ops_passcode_not_configured"
    monkeypatch.setenv("INSIDIOUS_OPS_PASSCODE", PASS)
    monkeypatch.delenv("FLAGSMITH_ORG_API_KEY")
    r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 503 and r.json()["error"] == "flagsmith_admin_not_configured"


def test_set_flag_patches_featurestate_and_posts_feed(client):
    with respx.mock() as mock:
        lookup = mock.get(STATES).mock(return_value=httpx.Response(200, json={"results": [
            {"id": 199, "feature": 1, "enabled": True, "identity": None, "feature_segment": 9},
            {"id": 101, "feature": 1, "enabled": True, "identity": None, "feature_segment": None},
        ], "next": None}))
        patch = mock.patch(f"{STATES}101/").mock(return_value=httpx.Response(200, json={
            "id": 101, "feature": 1, "enabled": False, "feature_state_value": None}))
        feed = mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["feed_posted"] is True and body["refresh_seconds"] == 5
    assert body["flag"]["name"] == "heavy_features" and body["flag"]["enabled"] is False
    assert body["flag"]["group"] == "Load shedding" and body["flag"]["is_parent"] is True

    assert lookup.calls.last.request.url.params["feature_name"] == "heavy_features"
    req = patch.calls.last.request
    assert req.headers["Authorization"] == "Api-Key org-test-key"
    assert json.loads(req.content) == {"enabled": False}
    assert json.loads(feed.calls.last.request.content) == {
        "source": "dashboard", "text": "heavy_features turned OFF from the dashboard"}


def test_set_flag_on_and_cache_invalidated(client):
    with respx.mock() as mock:
        feats = mock.get(FEATURES).mock(return_value=httpx.Response(200, json=FEATURE_ROWS))
        mock.get(STATES).mock(side_effect=states_api)
        patch = mock.patch(f"{STATES}107/").mock(return_value=httpx.Response(200, json={"id": 107, "enabled": True}))
        feed = mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert client.get("/ops/flags").status_code == 200
        r = client.post("/ops/flags/ai_agent", json={"enabled": True}, headers={"X-Ops-Passcode": PASS})
        assert client.get("/ops/flags").status_code == 200
    assert r.status_code == 200 and r.json()["flag"]["enabled"] is True
    assert json.loads(patch.calls.last.request.content) == {"enabled": True}
    assert json.loads(feed.calls.last.request.content)["text"] == "ai_agent turned ON from the dashboard"
    assert feats.call_count == 2  # the write dropped the 3 s cache


def test_set_payment_gateway_value(client):
    with respx.mock() as mock:
        mock.get(STATES).mock(side_effect=states_api)
        patch = mock.patch(f"{STATES}103/").mock(return_value=httpx.Response(200, json={
            "id": 103, "enabled": True, "feature_state_value": "cashfree"}))
        feed = mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        r = client.post("/ops/flags/payment_gateway", json={"enabled": True, "value": "cashfree"},
                        headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 200, r.text
    assert r.json()["flag"]["value"] == "cashfree"
    assert json.loads(patch.calls.last.request.content) == {"enabled": True, "feature_state_value": "cashfree"}
    assert json.loads(feed.calls.last.request.content)["text"] == "payment_gateway set to cashfree from the dashboard"


def test_set_payment_gateway_rejects_unknown_value(client):
    with respx.mock() as mock:
        r = client.post("/ops/flags/payment_gateway", json={"value": "stripe"}, headers={"X-Ops-Passcode": PASS})
        assert not mock.calls
    assert r.status_code == 400 and r.json()["error"] == "bad_value"


@pytest.mark.parametrize("name", ["db_read_url", "webhook_url", "admin_password"])
def test_db_read_url_value_edit_rejected(client, name):
    with respx.mock() as mock:
        r = client.post(f"/ops/flags/{name}", json={"value": "postgresql://evil:pw@attacker/db"},
                        headers={"X-Ops-Passcode": PASS})
        assert not mock.calls  # nothing reaches Flagsmith
    assert r.status_code == 403 and r.json()["error"] == "value_edit_forbidden"


@pytest.mark.parametrize("payload", [{}, {"enabled": "no"}, {"value": 3}, ["enabled"]])
def test_set_flag_bad_body(client, payload):
    with respx.mock() as mock:
        r = client.post("/ops/flags/heavy_features", json=payload, headers={"X-Ops-Passcode": PASS})
        assert not mock.calls
    assert r.status_code == 400


def test_set_flag_never_patches_wrong_feature_when_filter_ignored(client):
    """If Flagsmith ignored ?feature_name= and returned every state, match by feature id instead of taking row 0."""
    with respx.mock() as mock:
        mock.get(STATES).mock(return_value=httpx.Response(200, json=STATE_ROWS))
        mock.get(FEATURES).mock(return_value=httpx.Response(200, json=FEATURE_ROWS))
        patch = mock.patch(f"{STATES}107/").mock(return_value=httpx.Response(200, json={"id": 107, "enabled": False}))
        mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        r = client.post("/ops/flags/ai_agent", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 200 and patch.call_count == 1


def test_set_flag_unknown_feature_is_404(client):
    with respx.mock() as mock:
        mock.get(STATES).mock(return_value=httpx.Response(200, json=[]))
        r = client.post("/ops/flags/nope", json={"enabled": True}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 404 and r.json()["error"] == "unknown_flag"


def test_set_flag_survives_guardian_down(client):
    with respx.mock() as mock:
        mock.get(STATES).mock(side_effect=states_api)
        mock.patch(f"{STATES}101/").mock(return_value=httpx.Response(200, json={"id": 101, "enabled": False}))
        mock.post(FEED).mock(side_effect=httpx.ConnectError("no route"))
        r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["feed_posted"] is False


def test_set_flag_flagsmith_error_is_502(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(STATES).mock(side_effect=states_api)
        mock.patch(f"{STATES}101/").mock(return_value=httpx.Response(400, json={"detail": "versioning v2"}))
        feed = mock.post(FEED).mock(return_value=httpx.Response(200, json={"ok": True}))
        r = client.post("/ops/flags/heavy_features", json={"enabled": False}, headers={"X-Ops-Passcode": PASS})
    assert r.status_code == 502 and r.json()["error"] == "flagsmith_error"
    assert not feed.called


def test_ops_page_has_flag_panel(client):
    with respx.mock(assert_all_called=False):
        html = client.get("/ops").text
    assert 'id="ff-grid"' in html and "Feature flags · Flagsmith" in html and "X-Ops-Passcode" in html
