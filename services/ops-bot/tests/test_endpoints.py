import hashlib
import hmac
import json

from httpx import Response

from .conftest import FOUNDER, FS_SECRET, GUARDIAN, SARVAM, TOKEN


def test_notify_translates_and_texts(client_factory, mocked, rec):
    c = client_factory()
    assert c.post("/nb/notify", json={"text_en": "All good"}).status_code == 401
    r = c.post("/nb/notify", json={"text_en": "Sale in 30 min, scaled to 3", "severity": "info"},
               headers={"X-Insidious-Token": TOKEN})
    assert r.json() == {"ok": True}
    assert rec.types() == ["text"]
    body = rec.wa[0]["text"]["body"]
    assert body.startswith("अनुवाद: Sale in 30 min") and "(Sale in 30 min, scaled to 3)" in body


def test_notify_with_voice(client_factory, mocked, rec):
    c = client_factory()
    c.post("/nb/notify", json={"text_en": "Recovered", "severity": "info", "voice": True},
           headers={"X-Insidious-Token": TOKEN})
    assert rec.types() == ["audio", "text"]


def test_sarvam_402_falls_back_to_text(client_factory, mocked, rec):
    mocked.post(f"{SARVAM}/text-to-speech").mock(return_value=Response(402, json={"error": {"code": "insufficient"}}))
    c = client_factory()
    c.post("/nb/notify", json={"text_en": "Checkout recovered", "severity": "info", "voice": True},
           headers={"X-Insidious-Token": TOKEN})
    assert rec.types() == ["text"] and rec.uploads == 0
    # later messages skip Sarvam voice entirely while credits are out
    rec.wa.clear()
    c.post("/nb/notify", json={"text_en": "Still fine", "voice": True}, headers={"X-Insidious-Token": TOKEN})
    assert rec.types() == ["text"]


def test_sarvam_429_retries(client_factory, mocked, rec):
    route = mocked.post(f"{SARVAM}/translate").mock(side_effect=[
        Response(429), Response(503), Response(200, json={"translated_text": "ठीक है"})])
    c = client_factory()
    c.post("/nb/notify", json={"text_en": "ok"}, headers={"X-Insidious-Token": TOKEN})
    assert route.call_count == 3 and rec.wa[0]["text"]["body"].startswith("ठीक है")


FS_EVENT = {"event_type": "FLAG_UPDATED", "data": {
    "changed_by": "NudgeBee (API key)", "timestamp": "2026-11-25T14:03:00Z",
    "new_state": {"enabled": False, "feature": {"name": "heavy_features"}, "feature_state_value": None,
                  "identity": None, "feature_segment": None},
    "previous_state": {"enabled": True, "feature": {"name": "heavy_features"}, "feature_state_value": None,
                       "identity": None, "feature_segment": None}}}


def test_flagsmith_webhook_signature(client_factory, mocked, rec):
    c = client_factory()
    raw = json.dumps(FS_EVENT).encode()
    assert c.post("/flagsmith/webhook", content=raw, headers={"X-Flagsmith-Signature": "bad"}).status_code == 401
    assert c.post("/flagsmith/webhook", content=raw).status_code == 401
    sig = hmac.new(FS_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = c.post("/flagsmith/webhook", content=raw, headers={"X-Flagsmith-Signature": sig})
    assert r.status_code == 200
    assert r.json()["summary"] == "heavy_features turned OFF (by NudgeBee (API key))"
    assert rec.feed == [{"source": "flagsmith", "text": "heavy_features turned OFF (by NudgeBee (API key))"}]
    assert rec.wa[0]["to"] == FOUNDER and "heavy_features turned OFF" in rec.wa[0]["text"]["body"]


def test_flagsmith_remote_config_summary():
    from app.flows import summarize_flagsmith

    ev = {"event_type": "FLAG_UPDATED", "data": {"changed_by": "ravi@jeffy.in",
          "new_state": {"enabled": True, "feature": {"name": "payment_gateway"}, "feature_state_value": "cashfree"},
          "previous_state": {"enabled": True, "feature": {"name": "payment_gateway"}, "feature_state_value": "razorpay"}}}
    assert summarize_flagsmith(ev) == "payment_gateway set to cashfree (by ravi@jeffy.in)"


def test_status_intent_mock_mode(client_factory, mocked, rec):
    mocked.get(f"{GUARDIAN}/api/state").mock(return_value=Response(200, json={
        "slo": {"/checkout": {"p95_ms": 1840.0, "error_rate": 0.0, "target_p95_ms": 800, "breaching": True}},
        "rps": 420.5, "replicas": {"store-api": 1}, "flags": {"heavy_features": False, "recommendations": True}}))
    c = client_factory(sarvam_mock=True, whatsapp_dry_run=True)
    from .test_whatsapp import post_wa

    post_wa(c, {"from": FOUNDER, "id": "wamid.s1", "type": "text", "text": {"body": "abhi kya haal hai?"}})
    sent = c.app.state.ops.wa.sent
    assert [m["type"] for m in sent] == ["audio", "text"]
    assert "1840" in sent[1]["text"]["body"] and "heavy_features" in sent[1]["text"]["body"]


def test_why_flow_dry_run(client_factory, mocked, rec):
    c = client_factory(sarvam_mock=True, whatsapp_dry_run=True, nudgebee_dry_run=True)
    from .test_whatsapp import post_wa

    post_wa(c, {"from": FOUNDER, "id": "wamid.w1", "type": "text", "text": {"body": "site slow kyun hai?"}})
    ops = c.app.state.ops
    texts = [m["text"]["body"] for m in ops.wa.sent if m["type"] == "text"]
    assert any(t.startswith("NudgeBee: checking") for t in texts)
    assert any(m["type"] == "audio" for m in ops.wa.sent)
    assert [x["op"] for x in ops.nb.calls][0] == "ai_execute_investigation"


def test_brief_mock_mode(client_factory, mocked, rec):
    c = client_factory(sarvam_mock=True, whatsapp_dry_run=True)
    report = {"pass1": {"breaking_rps": 420}, "pass2": {"breaking_rps": 680}, "capacity_gain_if_shed": "+62%"}
    assert c.post("/api/brief", json={"kind": "readiness", "report": report}).status_code == 200
    sent = c.app.state.ops.wa.sent
    assert [m["type"] for m in sent] == ["audio", "text"]
    assert "420" in sent[1]["text"]["body"] and "680" in sent[1]["text"]["body"]


def test_legacy_festivekit_token_header_still_accepted(client_factory, mocked, rec):
    c = client_factory()
    assert c.post("/nb/notify", json={"text_en": "ok"}, headers={"X-FestiveKit-Token": TOKEN}).json() == {"ok": True}
    assert c.post("/nb/notify", json={"text_en": "ok"}, headers={"X-FestiveKit-Token": "wrong"}).status_code == 401
