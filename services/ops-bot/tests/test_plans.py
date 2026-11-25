"""v2: multi-option approvals from the Claude agent, option choice (button/voice/text), ask intent."""
import json
import re

import pytest
from httpx import Response

from .conftest import AGENT, FOUNDER, GRAPH, SARVAM, TOKEN, tone
from .test_whatsapp import create_pending, post_wa

PLAN_ID = "plan-20261125-140205-ab12"
PLAN = {
    "plan_id": PLAN_ID,
    "summary_en": "store-api p95 1.9s at 55 rps; CPU 98% of 500m limit",
    "founder_summary": "Lots of shoppers right now and checkout is slow. I suggest pausing product suggestions "
                       "and adding 2 more servers.",
    "options": [
        {"key": "recommended", "title_en": "Pause suggestions + add 2 servers", "cost_en": "About ₹12 more per hour",
         "impact_en": "Checkout fast again in about a minute", "actions": [{"type": "k8s.scale"}]},
        {"key": "alt1", "title_en": "Add a copy of the database for browsing", "cost_en": "About ₹150 more per day"},
        {"key": "reject", "title_en": "Do nothing for now"},
    ],
}
DONE = {"status": "done", "results": [
    {"type": "flagsmith.set_flag", "ok": True, "detail": "suggestions paused", "ts": "..."},
    {"type": "k8s.scale", "ok": True, "detail": "2 servers added", "ts": "..."}]}
HI_GO_AHEAD = "क्या मैं आगे बढ़ूँ?"


def agent_routes(mocked, decision=DONE, answer="Run 3 servers from 7 pm; that handles about 600 shoppers a second."):
    calls = {"decision": [], "ask": []}

    def dec(request):
        calls["decision"].append({"path": request.url.path, "body": json.loads(request.content),
                                  "token": request.headers.get("x-insidious-token")})
        return decision if isinstance(decision, Response) else Response(200, json=decision)

    def ask(request):
        calls["ask"].append({"body": json.loads(request.content), "token": request.headers.get("x-insidious-token")})
        return answer if isinstance(answer, Response) else Response(200, json={"answer_en": answer})

    mocked.post(url__regex=re.escape(AGENT) + r"/api/plans/[^/]+/decision").mock(side_effect=dec)
    mocked.post(f"{AGENT}/api/ask").mock(side_effect=ask)
    return calls


def tap(client, pid, key, msg_id, title=""):
    return post_wa(client, {"from": FOUNDER, "id": msg_id, "type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": f"opt:{pid}:{key}", "title": title or key}}})


def texts(msgs):
    return [m["text"]["body"] for m in msgs if m["type"] == "text"]


def llm_says(mocked, rec, intent, option="none"):
    """Make the Sarvam intent parser return a fixed intent/option (compose calls still get a sentence)."""
    def chat(request):
        body = json.loads(request.content)
        rec.chat.append(body)
        content = (json.dumps({"intent": intent, "confidence": 0.9, "option": option})
                   if body.get("response_format") else "ठीक है।")
        return Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                                                "finish_reason": "stop"}]})
    mocked.post(f"{SARVAM}/v1/chat/completions").mock(side_effect=chat)


# ---------- announce ----------

def test_plan_request_renders_option_buttons(client_factory, mocked, rec):
    c = client_factory()
    pid = create_pending(c, PLAN)
    assert rec.types() == ["audio", "interactive"]
    inter = rec.wa[1]["interactive"]
    buttons = inter["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == [f"opt:{pid}:recommended", f"opt:{pid}:alt1", f"opt:{pid}:reject"]
    titles = [b["reply"]["title"] for b in buttons]
    assert all(len(t) <= 20 for t in titles), titles
    assert [t[:2] for t in titles] == ["1 ", "2 ", "3 "] and "⭐" in titles[0] and titles[0].endswith("…")
    body = inter["body"]["text"]
    assert len(body) <= 1024
    assert "अनुवाद: Lots of shoppers right now" in body  # founder_summary, translated
    assert "1. अनुवाद: Pause suggestions + add 2 servers (सुझाव) — अनुवाद: About ₹12 more per hour" in body
    assert "2. अनुवाद: Add a copy of the database for browsing" in body  # full title in the body
    assert "3. अनुवाद: Do nothing for now" in body
    assert HI_GO_AHEAD in body
    # short voice note: summary + recommended option + go-ahead, in the founder's language
    spoken = rec.tts[0]["text"]
    assert len(spoken) <= 220 and spoken.endswith(HI_GO_AHEAD) and "मेरा सुझाव" in spoken
    assert rec.tts[0]["language_code"] == "hi-IN"
    p = c.get("/api/pending").json()[0]
    assert p["plan_id"] == PLAN_ID and [o["key"] for o in p["options"]] == ["recommended", "alt1", "reject"]
    assert PLAN_ID in rec.feed[-1]["text"]


def test_plan_request_english_founder_and_option_cap(client_factory, mocked, rec):
    c = client_factory(founder_language="en-IN")
    opts = PLAN["options"][:2] + [{"key": "alt2", "title_en": "Bigger database"},
                                  {"key": "alt3", "title_en": "Something else"}, PLAN["options"][2]]
    pid = create_pending(c, {**PLAN, "options": opts, "founder_summary": None})
    body = rec.wa[1]["interactive"]["body"]["text"]
    ids = [b["reply"]["id"] for b in rec.wa[1]["interactive"]["action"]["buttons"]]
    assert ids == [f"opt:{pid}:recommended", f"opt:{pid}:alt1", f"opt:{pid}:reject"]  # <= 3, reject kept last
    assert body.startswith(PLAN["summary_en"]) and body.endswith("Shall I go ahead?")  # summary_en fallback
    assert "अनुवाद" not in body and "(EN:" not in body


# ---------- choosing ----------

def test_option_button_calls_agent_and_completes_nudgebee(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked)
    pid = create_pending(c, {**PLAN, "execution_id": "exec-9", "task_id": "task-9"})
    rec.wa.clear()
    rec.feed.clear()
    tap(c, pid, "alt1", "wamid.opt.1", "2. अनुवाद: Add a…")
    (call,) = calls["decision"]
    assert call["path"] == f"/api/plans/{PLAN_ID}/decision" and call["token"] == TOKEN
    body = call["body"]
    assert (body["option"], body["channel"], body["actor"]) == ("alt1", "whatsapp-button", "founder")
    assert "hi-IN" in body["comments"]
    (done,) = rec.completes()
    assert (done["execution_id"], done["task_id"], done["status"]) == ("exec-9", "task-9", "approved")
    assert done["comments"].startswith("option=alt1")
    # ack, then the read-back (voice + text) from the agent's results
    sent_texts = texts(rec.wa)
    assert sent_texts[0].startswith("ठीक है, कर रहा हूँ: अनुवाद: Add a copy of the database")
    assert any("Done: suggestions paused, 2 servers added." in t for t in sent_texts[1:])
    assert "audio" in rec.types()
    assert c.get("/api/pending").json() == []
    assert any("alt1" in f["text"] and PLAN_ID in f["text"] for f in rec.feed)
    # a second tap does not decide twice
    tap(c, pid, "recommended", "wamid.opt.2")
    assert len(calls["decision"]) == 1 and len(rec.completes()) == 1


def test_reject_button_rejects_in_agent_and_nudgebee(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked, decision={"status": "rejected", "results": []})
    pid = create_pending(c, {**PLAN, "execution_id": "exec-9", "task_id": "task-9"})
    rec.wa.clear()
    tap(c, pid, "reject", "wamid.opt.r")
    assert calls["decision"][0]["body"]["option"] == "reject"
    (done,) = rec.completes()
    assert done["status"] == "rejected" and done["comments"].startswith("option=reject")
    assert any("Okay, I will not change anything." in t for t in texts(rec.wa))
    assert not any("कर रहा हूँ" in t for t in texts(rec.wa))  # no "on it" for reject
    assert c.get("/api/approvals").json()["all"][0]["status"] == "rejected"


def test_voice_pehla_wala_karo_picks_recommended(client_factory, mocked, rec):
    c = client_factory(sarvam_mock=True, sarvam_mock_transcript="pehla wala karo", whatsapp_dry_run=True)
    calls = agent_routes(mocked)
    create_pending(c, PLAN)
    post_wa(c, {"from": FOUNDER, "id": "wamid.v.1", "type": "audio",
                "audio": {"id": "media-x", "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    (call,) = calls["decision"]
    assert call["body"]["option"] == "recommended" and call["body"]["channel"] == "whatsapp-voice"
    assert "pehla wala karo" in call["body"]["comments"]
    assert rec.completes() == []  # no NudgeBee execution on this plan
    sent = c.app.state.ops.wa.sent
    assert any("Done: suggestions paused, 2 servers added." in t for t in texts(sent))
    assert sent[-1]["type"] == "text" and sent[-2]["type"] == "audio"  # spoken read-back, then text


def test_voice_llm_option_field_drives_choice(client_factory, mocked, rec):
    """Real (mocked HTTP) Sarvam path: STT transcript -> LLM intent with `option` -> agent decision."""
    c = client_factory()
    calls = agent_routes(mocked)
    create_pending(c, PLAN)
    mocked.get(f"{GRAPH}/media-in-2").mock(return_value=Response(200, json={
        "url": "https://lookaside.test/media-in-2", "mime_type": "audio/ogg; codecs=opus"}))
    mocked.get("https://lookaside.test/media-in-2").mock(return_value=Response(200, content=tone("ogg", 1.0)))
    mocked.post(f"{SARVAM}/speech-to-text").mock(return_value=Response(200, json={
        "request_id": "s2", "transcript": "doosre wala option theek rahega", "language_code": "hi-IN",
        "language_probability": 0.95}))
    llm_says(mocked, rec, "approve", "alt1")
    post_wa(c, {"from": FOUNDER, "id": "wamid.v.2", "type": "audio",
                "audio": {"id": "media-in-2", "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    schema = next(b for b in rec.chat if b.get("response_format"))["response_format"]["json_schema"]["schema"]
    assert "option" in schema["required"] and "ask" in schema["properties"]["intent"]["enum"]
    (call,) = calls["decision"]
    assert call["body"]["option"] == "alt1" and call["body"]["channel"] == "whatsapp-voice"
    assert "(hi-IN, 0.95)" in call["body"]["comments"]


def test_text_nahi_rejects_plan(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked, decision={"status": "rejected", "results": []})
    create_pending(c, PLAN)
    rec.wa.clear()
    post_wa(c, {"from": FOUNDER, "id": "wamid.t.nahi", "type": "text", "text": {"body": "nahi"}})
    (call,) = calls["decision"]
    assert call["body"]["option"] == "reject" and call["body"]["channel"] == "whatsapp-text"
    assert any("Okay, I will not change anything." in t for t in texts(rec.wa))


def test_multiple_pending_asks_which(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked)
    p1 = create_pending(c, PLAN)
    p2 = create_pending(c, {**PLAN, "plan_id": "plan-2"})
    rec.wa.clear()
    post_wa(c, {"from": FOUNDER, "id": "wamid.t.multi", "type": "text", "text": {"body": "haan karo"}})
    assert calls["decision"] == [] and rec.completes() == []
    assert rec.types() == ["text", "interactive", "interactive"]
    assert "2 मंज़ूरियाँ बाकी हैं" in rec.wa[0]["text"]["body"]
    ids = [[b["reply"]["id"] for b in m["interactive"]["action"]["buttons"]] for m in rec.wa[1:]]
    assert ids[0][0] == f"opt:{p1}:recommended" and ids[1][0] == f"opt:{p2}:recommended"


def test_agent_unreachable_apologizes_and_keeps_pending(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked, decision=Response(503, json={"detail": "starting"}))
    pid = create_pending(c, {**PLAN, "execution_id": "exec-9", "task_id": "task-9"})
    rec.wa.clear()
    tap(c, pid, "recommended", "wamid.fail.1")
    assert len(calls["decision"]) == 1
    assert rec.completes() == []  # nothing approved in NudgeBee either
    assert any(t.startswith("माफ़ कीजिए, अभी यह नहीं हो पाया। कुछ नहीं बदला।") for t in texts(rec.wa))
    assert [p["pending_id"] for p in c.get("/api/pending").json()] == [pid]  # founder can try again
    agent_routes(mocked)  # agent is back
    tap(c, pid, "recommended", "wamid.fail.2")
    assert len(rec.completes()) == 1 and c.get("/api/pending").json() == []


def test_agent_reports_failed_execution(client_factory, mocked, rec):
    c = client_factory(founder_language="en-IN")
    failed = {"status": "failed", "results": [
        {"type": "flagsmith.set_flag", "ok": True, "detail": "suggestions paused"},
        {"type": "k8s.scale", "ok": False, "detail": "the store is already at its 5 server limit"}]}
    agent_routes(mocked, decision=failed)
    pid = create_pending(c, PLAN)
    rec.wa.clear()
    tap(c, pid, "recommended", "wamid.fail.3")
    msg = texts(rec.wa)[-1]
    assert msg == ('Sorry, I could not finish "Pause suggestions + add 2 servers": the store is already at its '
                   "5 server limit. This part worked: suggestions paused.")
    assert c.get("/api/approvals").json()["all"][0]["status"] == "failed"


def test_plain_approval_still_uses_approve_reject(client_factory, mocked, rec):
    """Backward compatibility: no plan_id -> Approve/Reject buttons, NudgeBee only, agent untouched."""
    c = client_factory()
    calls = agent_routes(mocked)
    pid = create_pending(c, {"execution_id": "exec-1", "task_id": "task-1", "summary_en": "slow",
                             "numbers": {"p95_ms": 1840, "rps": 420}})
    ids = [b["reply"]["id"] for b in rec.wa[1]["interactive"]["action"]["buttons"]]
    assert ids == [f"approve:{pid}", f"reject:{pid}"]
    assert c.get("/api/pending").json()[0]["action"] == "shed_heavy_features"
    post_wa(c, {"from": FOUNDER, "id": "wamid.plain", "type": "text", "text": {"body": "haan karo"}})
    assert rec.completes()[0]["status"] == "approved" and calls["decision"] == []


# ---------- ask ----------

def test_ask_intent_forwards_to_agent(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked)
    llm_says(mocked, rec, "ask")
    post_wa(c, {"from": FOUNDER, "id": "wamid.ask", "type": "text",
                "text": {"body": "kal sale ke liye kitne servers chahiye?"}})
    (call,) = calls["ask"]
    assert call["token"] == TOKEN
    assert call["body"] == {"question": "अनुवाद: kal sale ke liye kitne servers chahiye?", "language": "hi-IN"}
    assert rec.types() == ["audio", "text"]
    assert rec.wa[1]["text"]["body"].startswith("अनुवाद: Run 3 servers from 7 pm")
    assert "(Run 3 servers from 7 pm" in rec.wa[1]["text"]["body"]  # English gloss


def test_ask_agent_down_apologizes(client_factory, mocked, rec):
    c = client_factory()
    agent_routes(mocked, answer=Response(500))
    llm_says(mocked, rec, "ask")
    post_wa(c, {"from": FOUNDER, "id": "wamid.ask.down", "type": "text", "text": {"body": "kitna kharcha hoga?"}})
    assert texts(rec.wa) == ["माफ़ कीजिए, अभी जवाब नहीं मिल पाया। एक मिनट बाद फिर पूछिए।"]


def test_why_uses_agent_when_nudgebee_dry_run(client_factory, mocked, rec):
    c = client_factory(sarvam_mock=True, whatsapp_dry_run=True, nudgebee_dry_run=True)
    calls = agent_routes(mocked, answer="Checkout is slow because suggestions use most of the CPU.")
    post_wa(c, {"from": FOUNDER, "id": "wamid.why.agent", "type": "text", "text": {"body": "site slow kyun hai?"}})
    ops = c.app.state.ops
    assert calls["ask"][0]["body"]["question"] == "site slow kyun hai?"
    assert ops.nb.calls == []  # NuBi not used
    assert any("suggestions use most of the CPU" in t for t in texts(ops.wa.sent))


def test_why_falls_back_to_agent_when_nudgebee_errors(client_factory, mocked, rec):
    c = client_factory()
    calls = agent_routes(mocked, answer="Too many shoppers for one server.")
    post_wa(c, {"from": FOUNDER, "id": "wamid.why.nberr", "type": "text", "text": {"body": "site slow kyun hai?"}})
    assert any("ai_execute_investigation" in x["query"] for x in rec.nb)  # tried NuBi first
    assert calls["ask"][0]["body"]["question"] == "अनुवाद: site slow kyun hai?"
    assert any("Too many shoppers" in t for t in texts(rec.wa))
    assert not any("NudgeBee is not reachable" in t for t in texts(rec.wa))


# ---------- units ----------

@pytest.mark.parametrize("text,option", [
    ("pehla wala karo", "recommended"), ("haan", "recommended"), ("ok", "recommended"), ("recommended", "recommended"),
    ("first one", "recommended"), ("doosra wala", "alt1"), ("second", "alt1"), ("2", "alt1"),
    ("teesra", "alt2"), ("third option", "alt2"), ("nahi", "reject"), ("no", "reject"), ("ruko", "reject"),
    ("mat karo", "reject"), ("नहीं", "reject"), ("पहला वाला", "recommended"), ("दूसरा", "alt1"), ("hello", None),
])
def test_keyword_option(text, option):
    from app.sarvam import keyword_option

    assert keyword_option(text) == option


def test_keyword_intent_ask_and_choice():
    from app.sarvam import keyword_intent

    assert keyword_intent("kal sale ke liye kitne servers chahiye")["intent"] == "ask"
    assert keyword_intent("database ka kharcha kitna hai?")["intent"] == "ask"
    assert keyword_intent("abhi kya haal hai?")["intent"] == "status"
    assert keyword_intent("site slow kyun hai?")["intent"] == "why"
    assert keyword_intent("pehla wala karo") == {"intent": "approve", "confidence": 0.6, "option": "recommended"}
    assert keyword_intent("teesra")["option"] == "alt2"


def test_short_title_and_positional_options():
    from app.flows import OpsBot, short_title

    for t in ("1. Pause suggestions + add 2 servers", "2. सुझाव रोकें और दो सर्वर जोड़ें", "x" * 40, "3. Do nothing"):
        out = short_title(t)
        assert len(out) <= 20 and out.startswith(t[:3])
    assert short_title("3. Do nothing") == "3. Do nothing"
    rec = {"options": [{"key": "recommended"}, {"key": "alt1"}, {"key": "reject"}]}
    assert OpsBot.resolve_option_key(rec, "alt1") == "alt1"
    assert OpsBot.resolve_option_key(rec, "alt2") == "reject"  # "teesra" = the third button
