import json

from httpx import Response

from .conftest import FOUNDER, GRAPH, SARVAM, TOKEN, sign, tone, wa_payload

APPROVAL = {"execution_id": "exec-1", "task_id": "task-1", "action": "shed_heavy_features",
            "summary_en": "Recommendations are burning CPU on the single store-api pod; checkout p95 is 1840 ms.",
            "numbers": {"p95_ms": 1840, "rps": 420, "gain": "+62%", "replicas_to": 3}}


def post_wa(client, msg):
    body = json.dumps(wa_payload(msg)).encode()
    return client.post("/whatsapp/webhook", content=body,
                       headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"})


def create_pending(client, payload=APPROVAL):
    r = client.post("/nb/approval-request", json=payload, headers={"X-Insidious-Token": TOKEN})
    assert r.status_code == 200, r.text
    return r.json()["pending_id"]


def test_healthz(client_factory):
    assert client_factory().get("/healthz").json() == {"ok": True}


def test_webhook_verify(client_factory):
    c = client_factory()
    r = c.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                           "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    r = c.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert r.status_code == 403


def test_bad_signature_rejected(client_factory, mocked, rec):
    c = client_factory()
    body = json.dumps(wa_payload({"from": FOUNDER, "id": "wamid.x", "type": "text", "text": {"body": "hi"}})).encode()
    assert c.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 401
    assert c.post("/whatsapp/webhook", content=body).status_code == 401
    assert rec.wa == []


def test_approval_request_token_and_fanout(client_factory, mocked, rec):
    c = client_factory()
    assert c.post("/nb/approval-request", json=APPROVAL).status_code == 401
    assert c.post("/nb/approval-request", json=APPROVAL, headers={"X-Insidious-Token": "wrong"}).status_code == 401
    pid = create_pending(c)
    # voice note first, then the text with Approve/Reject buttons
    assert rec.types() == ["audio", "interactive"]
    audio_msg, buttons = rec.wa
    assert audio_msg["to"] == FOUNDER and audio_msg["audio"] == {"id": "media-1", "voice": True}
    ids = [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]]
    assert ids == [f"approve:{pid}", f"reject:{pid}"]
    assert "चेकआउट" in buttons["interactive"]["body"]["text"]
    # TTS always names the model and speaks the founder's language
    assert rec.tts[0]["model"] == "bulbul:v4-flash" and rec.tts[0]["language_code"] == "hi-IN"
    # LLM gets enough room for reasoning tokens
    assert all(b["max_tokens"] >= 1024 for b in rec.chat)
    assert rec.feed and "approve" in rec.feed[0]["text"]
    pending = c.get("/api/pending").json()
    assert [p["pending_id"] for p in pending] == [pid]


def test_voice_note_approval(client_factory, mocked, rec):
    c = client_factory()
    pid = create_pending(c)
    rec.wa.clear()
    ogg = tone("ogg", 1.0)
    mocked.get(f"{GRAPH}/media-in-1").mock(return_value=Response(200, json={
        "url": "https://lookaside.test/media-in-1", "mime_type": "audio/ogg; codecs=opus"}))
    dl = mocked.get("https://lookaside.test/media-in-1").mock(return_value=Response(200, content=ogg))
    stt = mocked.post(f"{SARVAM}/speech-to-text").mock(return_value=Response(200, json={
        "request_id": "s1", "transcript": "haan karo", "language_code": "hi-IN", "language_probability": 0.97}))

    r = post_wa(c, {"from": FOUNDER, "id": "wamid.voice1", "timestamp": "1", "type": "audio",
                    "audio": {"id": "media-in-1", "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    assert r.status_code == 200
    assert dl.calls[0].request.headers["authorization"] == "Bearer wa-test"
    form = stt.calls[0].request.content
    assert b'name="model"' in form and b"saaras:v4" in form and b"codemix" in form
    assert b'name="language_code"' not in form  # auto-detect
    assert stt.calls[0].request.headers["api-subscription-key"] == "sk-test"

    (done,) = rec.completes()
    assert done["execution_id"] == "exec-1" and done["task_id"] == "task-1" and done["status"] == "approved"
    assert "voice: haan karo (hi-IN, 0.97)" in done["comments"]
    assert c.get("/api/pending").json() == []
    assert "audio" in rec.types()  # spoken read-back
    assert any("approved" in f["text"] for f in rec.feed)


def test_button_reply_approve_and_reject(client_factory, mocked, rec):
    c = client_factory()
    pid1 = create_pending(c)
    pid2 = create_pending(c, {**APPROVAL, "execution_id": "exec-2", "task_id": "task-2", "action": "scale_out"})
    for pid, kind in ((pid1, "approve"), (pid2, "reject")):
        r = post_wa(c, {"from": FOUNDER, "id": f"wamid.btn.{kind}", "type": "interactive", "interactive": {
            "type": "button_reply", "button_reply": {"id": f"{kind}:{pid}", "title": kind.title()}}})
        assert r.status_code == 200
    a, b = rec.completes()
    assert (a["execution_id"], a["task_id"], a["status"]) == ("exec-1", "task-1", "approved")
    assert (b["execution_id"], b["task_id"], b["status"]) == ("exec-2", "task-2", "rejected")
    assert a["comments"].startswith("button:")
    # pressing again does not complete twice
    post_wa(c, {"from": FOUNDER, "id": "wamid.btn.again", "type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": f"approve:{pid1}", "title": "Approve"}}})
    assert len(rec.completes()) == 2


def test_missing_ids_resolved_from_waiting_task(client_factory, mocked, rec):
    c = client_factory()
    pid = create_pending(c, {**APPROVAL, "execution_id": None, "task_id": None, "workflow_id": "wf-77"})
    post_wa(c, {"from": FOUNDER, "id": "wamid.btn.r", "type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": f"approve:{pid}", "title": "Approve"}}})
    (done,) = rec.completes()
    assert done["execution_id"] == "exec-42" and done["task_id"] == "approve-task-7"
    listed = [x for x in rec.nb if "workflow_list_executions" in x["query"]][0]
    assert listed["variables"]["id"] == "wf-77" and listed["variables"]["accountId"] == "acc-1"


def test_voice_approval_needs_exactly_one_pending(client_factory, mocked, rec):
    c = client_factory()
    create_pending(c)
    create_pending(c, {**APPROVAL, "execution_id": "exec-2"})
    rec.wa.clear()
    post_wa(c, {"from": FOUNDER, "id": "wamid.t1", "type": "text", "text": {"body": "haan karo"}})
    assert rec.completes() == []
    assert rec.types().count("interactive") == 2  # asks which one


def test_non_founder_and_dedupe(client_factory, mocked, rec):
    c = client_factory()
    msg = {"from": "911111111111", "id": "wamid.stranger", "type": "text", "text": {"body": "hello"}}
    post_wa(c, msg)
    post_wa(c, msg)  # Meta retry -> deduped
    assert len(rec.wa) == 1 and rec.wa[0]["to"] == "911111111111"
    assert "private ops line" in rec.wa[0]["text"]["body"]


def test_username_only_sender_uses_from_user_id(client_factory, mocked, rec):
    c = client_factory(founder_user_id="IN.123456789")
    pid = create_pending(c)
    post_wa(c, {"from_user_id": "IN.123456789", "id": "wamid.u1", "type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": f"approve:{pid}", "title": "Approve"}}})
    assert len(rec.completes()) == 1
