"""Tanglish founder style: switch messages, Flagsmith/env sources, Tanglish compose/translate/voice, detection."""
import json
import string

import pytest
from httpx import Response

from .conftest import FOUNDER, GUARDIAN, SARVAM, TOKEN
from .test_plans import llm_says, texts
from .test_whatsapp import APPROVAL, create_pending, post_wa

TANGLISH_OK = "Seri, ippo irundhu Tanglish-la reply panren."


def say(client, text, msg_id):
    return post_wa(client, {"from": FOUNDER, "id": msg_id, "type": "text", "text": {"body": text}})


def voice(client, msg_id):
    return post_wa(client, {"from": FOUNDER, "id": msg_id, "type": "audio",
                            "audio": {"id": "media-x", "mime_type": "audio/ogg; codecs=opus", "voice": True}})


def compose_bodies(rec):
    return [b for b in rec.chat if not b.get("response_format")]


# ---------- units ----------

@pytest.mark.parametrize("text,want", [
    ("tanglish", ("ta-IN", "tanglish")), ("Tanglish!", ("ta-IN", "tanglish")),
    ("reply in tanglish please", ("ta-IN", "tanglish")),
    ("tamil", ("ta-IN", "native")), ("Thamizh", ("ta-IN", "native")), ("தமிழ்", ("ta-IN", "native")),
    ("tamil la pesu", ("ta-IN", "native")), ("Tamil-la pesunga", ("ta-IN", "native")),
    ("hindi", ("hi-IN", "native")), ("हिंदी", ("hi-IN", "native")), ("hindi mein baat karo", ("hi-IN", "native")),
    ("English", ("en-IN", "native")), ("speak in english.", ("en-IN", "native")),
    ("telugu", ("te-IN", "native")), ("తెలుగు", ("te-IN", "native")), ("kannada", ("kn-IN", "native")),
    ("ಕನ್ನಡ", ("kn-IN", "native")), ("malayalam", ("ml-IN", "native")), ("മലയാളം", ("ml-IN", "native")),
    ("bengali", ("bn-IN", "native")), ("বাংলা", ("bn-IN", "native")), ("marathi", ("mr-IN", "native")),
    ("मराठी", ("mr-IN", "native")), ("gujarati", ("gu-IN", "native")), ("ગુજરાતી", ("gu-IN", "native")),
    # not switches: content words, too long, two languages, no language
    ("tamil customers ku enna aachu", None), ("please reply in english from now on okay", ("en-IN", "native")),
    ("hindi or english", None), ("haan karo", None), ("", None),
])
def test_parse_switch(text, want):
    from app.flows import parse_switch

    assert parse_switch(text) == want


@pytest.mark.parametrize("text,want", [
    ("site enna aachu da", True), ("Kadai epdi irukku?", True), ("seri pannunga", True),
    ("romba slow ah irukku machan", True), ("enna", False), ("haan karo", False),
    ("kal sale ke liye kitne servers chahiye?", False), ("why is the site slow", False),
    ("என்ன ஆச்சு", False),  # Tamil script is native Tamil, not Tanglish
])
def test_looks_tanglish(text, want):
    from app.flows import looks_tanglish

    assert looks_tanglish(text) is want


def test_resolve_voice_priority():
    from app.flows import resolve_voice

    detected = {"language": "hi-IN", "style": None, "switched": False}
    assert resolve_voice(detected, {}, "native") == ("hi-IN", "native")
    assert resolve_voice(detected, {}, "tanglish") == ("ta-IN", "tanglish")  # env default
    assert resolve_voice({**detected, "style": "native"}, {}, "tanglish") == ("hi-IN", "native")  # detected > env
    assert resolve_voice(detected, {"style": "tanglish"}, "native") == ("ta-IN", "tanglish")  # Flagsmith > env
    assert resolve_voice(detected, {"language": "te-IN"}, "tanglish") == ("te-IN", "native")
    assert resolve_voice({**detected, "style": "tanglish"}, {"language": "ta-IN"}, "native") == ("ta-IN", "tanglish")
    assert resolve_voice(detected, {"language": "tanglish"}, "native") == ("ta-IN", "tanglish")
    pinned = {"language": "en-IN", "style": "native", "switched": True}
    assert resolve_voice(pinned, {"language": "ta-IN", "style": "tanglish"}, "tanglish") == ("en-IN", "native")


def test_phrases_have_tanglish_for_every_key():
    from app.flows import PHRASES, script_language

    def fields(t):
        return {f for _, f, _, _ in string.Formatter().parse(t) if f}

    for key, p in PHRASES.items():
        assert p.get("ta-Latn"), key
        assert script_language(p["ta-Latn"]) is None, key  # Latin letters only
        assert fields(p["ta-Latn"]) <= fields(p["en"]), key
        assert p["ta-Latn"] != p["en"], key


# ---------- switch messages ----------

def test_text_switch_to_tanglish_sets_persists_and_speaks_tamil_script(client_factory, mocked, rec, tmp_path):
    c = client_factory()
    say(c, "Tanglish", "wamid.sw.1")
    ops = c.app.state.ops
    assert ops.store.founder["style"] == "tanglish" and ops.store.founder["switched"] is True
    assert (ops.lang, ops.style) == ("ta-IN", "tanglish")
    assert rec.chat == []  # a switch is handled before intent parsing
    assert rec.types() == ["audio", "text"] and rec.wa[1]["text"]["body"] == TANGLISH_OK
    # voice: Tanglish -> Tamil script (transliterate) -> Bulbul ta-IN
    assert rec.order == ["transliterate", "tts"]
    assert rec.translit[0] == {"input": TANGLISH_OK, "source_language_code": "en-IN",
                               "target_language_code": "ta-IN", "spoken_form": True}
    assert rec.tts[0]["text"] == "தமிழ்: " + TANGLISH_OK
    assert rec.tts[0]["language_code"] == "ta-IN" and rec.tts[0]["model"] == "bulbul:v4-flash"
    assert any("switched replies to Tanglish" in f["text"] for f in rec.feed)
    # persisted next to language
    from app.store import Store

    saved = Store(str(tmp_path / "data"), "hi-IN").founder
    assert (saved["language"], saved["style"], saved["switched"]) == ("ta-IN", "tanglish", True)
    assert c.get("/api/approvals").json()["style"] == "tanglish"


@pytest.mark.parametrize("transcript,lang,style,reply", [
    ("हिंदी", "hi-IN", "native", "ठीक है, अब से मैं हिंदी में जवाब दूँगा।"),
    ("tamil la pesu", "ta-IN", "native", "சரி, இனிமேல் தமிழில் பதில் சொல்கிறேன்."),
    ("English.", "en-IN", "native", "Okay, I will reply in English from now on."),
    ("Tanglish", "ta-IN", "tanglish", TANGLISH_OK),
])
def test_voice_switch(client_factory, mocked, rec, transcript, lang, style, reply):
    c = client_factory(sarvam_mock=True, sarvam_mock_transcript=transcript, sarvam_mock_language="hi-IN",
                       whatsapp_dry_run=True, founder_style="tanglish" if style != "tanglish" else "native")
    voice(c, "wamid.vsw." + lang)
    ops = c.app.state.ops
    assert (ops.lang, ops.style) == (lang, style) and ops.store.founder["switched"]
    sent = ops.wa.sent
    assert [m["type"] for m in sent] == ["audio", "text"] and texts(sent) == [reply]


def test_switch_beats_flagsmith_and_detection(client_factory, mocked, rec):
    c = client_factory(flagsmith_server_key="fs-test")
    ops = c.app.state.ops
    ops.voice._fetch_sync = lambda: {"speaker": "", "language": "ta-IN", "style": "tanglish"}
    c.post("/nb/notify", json={"text_en": "Sale in 30 min"}, headers={"X-Insidious-Token": TOKEN})
    assert rec.translate[-1]["model"] == "mayura:v1"  # Flagsmith style honoured
    assert texts(rec.wa)[-1].startswith("Tanglish: Sale in 30 min")
    say(c, "hindi", "wamid.sw.hi")
    assert texts(rec.wa)[-1] == "ठीक है, अब से मैं हिंदी में जवाब दूँगा।"
    say(c, "enna da, kadai epdi irukku", "wamid.sw.tl")  # Tanglish detection does not override the switch
    assert (ops.lang, ops.style) == ("hi-IN", "native")
    c.post("/nb/notify", json={"text_en": "All quiet"}, headers={"X-Insidious-Token": TOKEN})
    assert rec.translate[-1] == {"input": "All quiet", "source_language_code": "en-IN",
                                 "target_language_code": "hi-IN", "model": "sarvam-translate:v1"}


def test_flagsmith_language_is_honoured(client_factory, mocked, rec):
    c = client_factory(flagsmith_server_key="fs-test")
    c.app.state.ops.voice._fetch_sync = lambda: {"language": "te-IN"}
    c.post("/nb/notify", json={"text_en": "Recovered", "voice": True}, headers={"X-Insidious-Token": TOKEN})
    assert rec.translate[0]["target_language_code"] == "te-IN"
    assert rec.tts[0]["language_code"] == "te-IN" and rec.translit == []


# ---------- Tanglish generation ----------

def test_tanglish_compose_translate_and_voice(client_factory, mocked, rec):
    from app.flows import TANGLISH_GOAL

    c = client_factory(founder_style="tanglish")
    create_pending(c, APPROVAL)
    (compose,) = compose_bodies(rec)
    assert TANGLISH_GOAL in compose["messages"][-1]["content"]
    assert "Tanglish" in compose["messages"][0]["content"] and "native script" not in compose["messages"][0]["content"]
    (tr,) = rec.translate  # English fallback -> Tanglish
    assert {k: tr[k] for k in ("model", "mode", "output_script", "source_language_code", "target_language_code")} == {
        "model": "mayura:v1", "mode": "code-mixed", "output_script": "roman",
        "source_language_code": "en-IN", "target_language_code": "ta-IN"}
    assert tr["input"].startswith("Checkout is slow: 1840 ms")
    assert rec.order == ["transliterate", "tts"] and rec.tts[0]["language_code"] == "ta-IN"
    assert rec.tts[0]["text"].startswith("தமிழ்: ")


def test_tanglish_translate_rejected_falls_back_to_native(client_factory, mocked, rec):
    def translate(request):
        body = json.loads(request.content)
        rec.translate.append(body)
        if body["model"] == "mayura:v1":
            return Response(400, json={"error": {"message": "invalid output_script"}})
        return Response(200, json={"translated_text": "விற்பனை 30 நிமிடத்தில்"})

    mocked.post(f"{SARVAM}/translate").mock(side_effect=translate)
    c = client_factory(founder_style="tanglish")
    c.post("/nb/notify", json={"text_en": "Sale in 30 min"}, headers={"X-Insidious-Token": TOKEN})
    assert [b["model"] for b in rec.translate] == ["mayura:v1", "sarvam-translate:v1"]
    assert "mode" not in rec.translate[1] and "output_script" not in rec.translate[1]
    assert texts(rec.wa)[0].startswith("விற்பனை 30 நிமிடத்தில்")
    c.post("/nb/notify", json={"text_en": "Still fine"}, headers={"X-Insidious-Token": TOKEN})
    assert [b["model"] for b in rec.translate[2:]] == ["sarvam-translate:v1"]  # rejection remembered


def test_tanglish_voice_falls_back_to_llm_rewrite_then_raw_text(client_factory, mocked, rec):
    mocked.post(f"{SARVAM}/transliterate").mock(return_value=Response(500, json={"error": "down"}))

    def chat(request):
        body = json.loads(request.content)
        rec.chat.append(body)
        rewrite = "Rewrite" in body["messages"][0]["content"]
        content = "சரி, store இப்போ நல்லா இருக்கு" if rewrite else "Seri, store ippo nalla irukku"
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    route = mocked.post(f"{SARVAM}/v1/chat/completions").mock(side_effect=chat)
    c = client_factory(founder_style="tanglish")
    c.post("/nb/notify", json={"text_en": "Recovered", "voice": True}, headers={"X-Insidious-Token": TOKEN})
    assert "Tamil script" in rec.chat[-1]["messages"][0]["content"]
    assert rec.chat[-1]["messages"][1]["content"] == "Seri, store ippo nalla irukku"
    assert rec.tts[-1]["text"] == "சரி, store இப்போ நல்லா இருக்கு" and rec.tts[-1]["language_code"] == "ta-IN"
    assert texts(rec.wa)[-1].startswith("Seri, store ippo nalla irukku")
    # transliterate and LLM both down: speak the Tanglish as is, still in ta-IN
    route.mock(return_value=Response(500, json={"error": "down"}))
    c.post("/nb/notify", json={"text_en": "Recovered again", "voice": True}, headers={"X-Insidious-Token": TOKEN})
    assert rec.tts[-1]["text"] == "Tanglish: Recovered again" and rec.tts[-1]["language_code"] == "ta-IN"


def test_typed_tanglish_detected_for_this_and_later_replies(client_factory, mocked, rec):
    from app.flows import TANGLISH_GOAL

    c = client_factory()
    llm_says(mocked, rec, "smalltalk")
    say(c, "enna da, kadai epdi irukku", "wamid.tl.1")
    ops = c.app.state.ops
    assert (ops.store.founder["language"], ops.store.founder["style"]) == ("ta-IN", "tanglish")
    assert not ops.store.founder["switched"]
    assert TANGLISH_GOAL in compose_bodies(rec)[-1]["messages"][-1]["content"]
    assert rec.translate[-1]["output_script"] == "roman" and rec.tts[-1]["language_code"] == "ta-IN"
    say(c, "ok thanks", "wamid.tl.2")  # plain English text: stays Tanglish
    assert ops.style == "tanglish" and TANGLISH_GOAL in compose_bodies(rec)[-1]["messages"][-1]["content"]
    say(c, "नमस्ते", "wamid.tl.3")  # Devanagari: back to Hindi (detection, not a switch)
    assert (ops.lang, ops.style) == ("hi-IN", "native")


def test_tanglish_fixed_phrases_replace_hindi_only_fallbacks(client_factory, mocked, rec):
    mocked.get(f"{GUARDIAN}/api/state").mock(return_value=Response(500))
    c = client_factory(sarvam_mock=True, whatsapp_dry_run=True, founder_style="tanglish")
    say(c, "seri pannunga", "wamid.ph.1")  # approve, but nothing is pending
    say(c, "status", "wamid.ph.2")  # guardian unreachable
    assert texts(c.app.state.ops.wa.sent) == ["Ippo unga approval-ku edhuvum wait pannala.",
                                              "Ippo store monitor-a reach panna mudiyala."]
