import base64
import hashlib
import hmac
import json
import os
import subprocess
import tempfile

# Keep the module-level app (created at import) from writing into the repo.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="opsbot-import-"))
os.environ.setdefault("WHATSAPP_DRY_RUN", "1")
os.environ.setdefault("NUDGEBEE_DRY_RUN", "1")
os.environ.setdefault("SARVAM_MOCK", "1")

import pytest  # noqa: E402
import respx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

FOUNDER = "919800000000"
GRAPH = "https://graph.test/v27.0"
SARVAM = "https://sarvam.test"
NB = "https://nb.test/api/graphql"
GUARDIAN = "http://guardian.test"
AGENT = "http://agent.test"
APP_SECRET = "appsecret"
FS_SECRET = "fsecret"
TOKEN = "tok"


def make_settings(tmp_path, **over) -> Settings:
    base = dict(
        whatsapp_token="wa-test", whatsapp_phone_number_id="PNID", whatsapp_verify_token="verify-me",
        whatsapp_app_secret=APP_SECRET, whatsapp_api_version="v27.0", whatsapp_base_url="https://graph.test",
        whatsapp_dry_run=False, founder_phone=FOUNDER, founder_language="hi-IN", founder_style="native",
        sarvam_api_key="sk-test", sarvam_base_url=SARVAM, sarvam_mock=False, sarvam_retry_base_s=0.0,
        nudgebee_url="https://nb.test", nudgebee_api_token="nb-test", nudgebee_account_id="acc-1",
        nudgebee_wf_incident_id="wf-incident", nudgebee_dry_run=False,
        flagsmith_webhook_secret=FS_SECRET, guardian_url=GUARDIAN, agent_url=AGENT, internal_token=TOKEN,
        data_dir=str(tmp_path / "data"), dry_run_audio_dir=str(tmp_path / "outbox"),
        why_poll_interval_s=0.01, why_timeout_s=2.0, why_update_min_gap_s=0.0, approval_resolve_gap_s=0.0,
    )
    base.update(over)
    return Settings(**base)


def tone(fmt: str = "wav", seconds: float = 1.0) -> bytes:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={seconds}", "-ac", "1"]
    args += ["-c:a", "libopus", "-f", "ogg", "pipe:1"] if fmt == "ogg" else ["-f", "wav", "pipe:1"]
    return subprocess.run(args, check=True, capture_output=True).stdout


WAV_B64 = base64.b64encode(tone("wav", 0.5)).decode()


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def wa_payload(msg: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
        "contacts": [{"wa_id": msg.get("from", ""), "profile": {"name": "Ravi"}}], "messages": [msg]}}]}]}


class Recorder:
    """Captures outbound WhatsApp messages, NudgeBee GraphQL calls and guardian feed posts."""

    def __init__(self):
        self.wa: list[dict] = []
        self.uploads = 0
        self.nb: list[dict] = []
        self.feed: list[dict] = []
        self.chat: list[dict] = []
        self.tts: list[dict] = []
        self.translate: list[dict] = []
        self.translit: list[dict] = []
        self.order: list[str] = []  # Sarvam call order (transliterate / tts)

    def types(self):
        return [m["type"] for m in self.wa]

    def completes(self):
        return [c["variables"]["request"] for c in self.nb if "workflow_complete_approval" in c["query"]]


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def mocked(rec):
    """respx router with sensible defaults for every external API. Tests override routes as needed."""
    with respx.mock(assert_all_called=False) as router:
        def wa_send(request):
            rec.wa.append(json.loads(request.content))
            return Response(200, json={"messages": [{"id": f"wamid.out.{len(rec.wa)}"}]})

        def wa_upload(request):
            rec.uploads += 1
            assert b"messaging_product" in request.content and b"audio/ogg" in request.content
            return Response(200, json={"id": f"media-{rec.uploads}"})

        def chat(request):
            body = json.loads(request.content)
            rec.chat.append(body)
            if body.get("response_format"):
                text = body["messages"][-1]["content"].lower()
                intent = "approve" if ("haan" in text or "approve" in text) else (
                    "reject" if "nahi" in text else ("status" if "haal" in text else "why"))
                content = json.dumps({"intent": intent, "confidence": 0.93})
            else:
                content = "चेकआउट धीमा है। क्या मैं भारी फ़ीचर बंद कर दूँ?"
            return Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                                                    "finish_reason": "stop"}]})

        def tts(request):
            body = json.loads(request.content)
            rec.tts.append(body)
            rec.order.append("tts")
            return Response(200, json={"request_id": "r1", "audios": [WAV_B64]})

        def feed(request):
            rec.feed.append(json.loads(request.content))
            return Response(200, json={"ok": True})

        def nb(request):
            body = json.loads(request.content)
            rec.nb.append(body)
            q = body["query"]
            if "workflow_complete_approval" in q:
                return Response(200, json={"data": {"workflow_complete_approval": {"status": "success", "message": "ok"}}})
            if "workflow_list_executions" in q:
                return Response(200, json={"data": {"workflow_list_executions": {"next_page_token": None, "executions": [
                    {"id": "exec-old", "status": "COMPLETED"}, {"id": "exec-42", "status": "RUNNING"}]}}})
            if "workflow_get_execution" in q:
                return Response(200, json={"data": {"workflow_get_execution": {"id": "exec-42", "status": "RUNNING", "tasks": [
                    {"id": "classify", "type": "llm.classify", "status": "COMPLETED"},
                    {"id": "approve-task-7", "type": "core.approval", "status": "WAITING"}]}}})
            return Response(200, json={"errors": [{"message": "unexpected op"}]})

        router.post(f"{GRAPH}/PNID/messages").mock(side_effect=wa_send)
        router.post(f"{GRAPH}/PNID/media").mock(side_effect=wa_upload)
        router.post(f"{SARVAM}/v1/chat/completions").mock(side_effect=chat)
        router.post(f"{SARVAM}/text-to-speech").mock(side_effect=tts)
        def translate(request):
            body = json.loads(request.content)
            rec.translate.append(body)
            prefix = "Tanglish: " if body.get("output_script") == "roman" else "अनुवाद: "
            return Response(200, json={"translated_text": prefix + body["input"]})

        def transliterate(request):
            body = json.loads(request.content)
            rec.translit.append(body)
            rec.order.append("transliterate")
            return Response(200, json={"request_id": "t1", "transliterated_text": "தமிழ்: " + body["input"],
                                       "source_language_code": body["source_language_code"]})

        router.post(f"{SARVAM}/translate").mock(side_effect=translate)
        router.post(f"{SARVAM}/transliterate").mock(side_effect=transliterate)
        router.post(f"{GUARDIAN}/api/feed").mock(side_effect=feed)
        router.post(NB).mock(side_effect=nb)
        yield router


@pytest.fixture
def client_factory(tmp_path):
    clients = []

    def make(**over):
        app = create_app(make_settings(tmp_path, **over))
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
