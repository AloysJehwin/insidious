"""ops-bot HTTP surface (port 8003). See docs/CONTRACTS.md."""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings
from .flows import OpsBot, summarize_flagsmith
from .log import log
from .whatsapp import parse_webhook, verify_meta_signature


class PlanOption(BaseModel):
    key: str  # recommended | alt1 | alt2 | reject
    title_en: str = ""
    impact_en: str = ""
    cost_en: str = ""


class ApprovalRequest(BaseModel):
    execution_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    action: str | None = None  # defaults to shed_heavy_features (plain) / agent_plan (with plan_id)
    summary_en: str = ""
    numbers: dict = Field(default_factory=dict)
    # v2: multi-option plans from the Claude agent (docs/CONTRACTS.md "ops-bot: multi-option approvals")
    plan_id: str | None = None
    founder_summary: str | None = None
    founder_summary_en: str | None = None  # accepted alias (plan JSON field name)
    options: list[PlanOption] = Field(default_factory=list)  # <= 3; extra ones are dropped, reject kept


class NotifyRequest(BaseModel):
    text_en: str
    severity: str = "info"
    voice: bool = False


class BriefRequest(BaseModel):
    kind: str = "readiness"
    report: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    ops = OpsBot(s)
    app = FastAPI(title="Insidious ops-bot")
    app.state.ops = ops
    dev_mode = s.whatsapp_dry_run or s.nudgebee_dry_run
    log("startup", sarvam_mock=s.sarvam_mock, whatsapp_dry_run=s.whatsapp_dry_run,
        nudgebee_dry_run=s.nudgebee_dry_run, founder_configured=bool(s.founder_phone),
        flagsmith_voice=bool(s.flagsmith_server_key), data_dir=s.data_dir)

    def check_token(request: Request, required: bool = True) -> None:
        # X-FestiveKit-Token is the pre-rename header; still accepted (a NudgeBee workflow sends it).
        got = request.headers.get("x-insidious-token") or request.headers.get("x-festivekit-token")
        if not s.internal_token:
            if dev_mode or not required:
                return
            log("internal_token_missing", note="INSIDIOUS_INTERNAL_TOKEN is not set; rejecting")
            raise HTTPException(401, "internal token not configured")
        if got is None and not required:
            return
        if not got or not hmac.compare_digest(got, s.internal_token):
            raise HTTPException(401, "bad X-Insidious-Token")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ---------- WhatsApp ----------
    @app.get("/whatsapp/webhook")
    async def wa_verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == s.whatsapp_verify_token:
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/whatsapp/webhook")
    async def wa_webhook(request: Request, bg: BackgroundTasks):
        raw = await request.body()
        sig = request.headers.get("x-hub-signature-256")
        if s.whatsapp_app_secret:
            if not verify_meta_signature(s.whatsapp_app_secret, raw, sig):
                log("wa_bad_signature")
                raise HTTPException(401, "bad signature")
        elif not s.whatsapp_dry_run:
            log("wa_app_secret_missing", note="WHATSAPP_APP_SECRET not set; rejecting webhook")
            raise HTTPException(403, "app secret not configured")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "bad json")
        items = parse_webhook(payload)
        fresh = [m for m in items if not ops.store.seen_before(m.msg_id)]
        if len(fresh) < len(items):
            log("wa_duplicates_skipped", count=len(items) - len(fresh))
        if fresh:
            bg.add_task(ops.process_inbound, fresh)
        return {"ok": True}

    # ---------- NudgeBee ----------
    @app.post("/nb/approval-request")
    async def nb_approval(req: ApprovalRequest, request: Request, bg: BackgroundTasks):
        check_token(request)
        rec = await ops.approval_request(req.model_dump())
        bg.add_task(ops.announce_approval, rec["pending_id"])
        return {"ok": True, "pending_id": rec["pending_id"]}

    @app.post("/nb/notify")
    async def nb_notify(req: NotifyRequest, request: Request, bg: BackgroundTasks):
        check_token(request)
        bg.add_task(ops.notify, req.text_en, req.severity, req.voice)
        return {"ok": True}

    # ---------- Flagsmith ----------
    @app.post("/flagsmith/webhook")
    async def flagsmith_webhook(request: Request, bg: BackgroundTasks):
        raw = await request.body()
        if s.flagsmith_webhook_secret:
            got = request.headers.get("x-flagsmith-signature", "")
            want = hmac.new(s.flagsmith_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
            if not got or not hmac.compare_digest(want, got.strip().lower()):
                log("flagsmith_bad_signature")
                raise HTTPException(401, "bad signature")
        else:
            log("flagsmith_webhook_unsigned", note="FLAGSMITH_WEBHOOK_SECRET not set; accepting unsigned webhook")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "bad json")
        summary = summarize_flagsmith(payload)
        log("flagsmith_event", summary=summary)
        bg.add_task(ops.flag_event, summary)
        return {"ok": True, "summary": summary}

    # ---------- CLI / cockpit ----------
    @app.post("/api/brief")
    async def api_brief(req: BriefRequest, request: Request, bg: BackgroundTasks):
        check_token(request, required=False)
        bg.add_task(ops.brief, req.kind, req.report)
        return {"ok": True}

    @app.get("/api/pending")
    async def api_pending():
        return ops.store.pending()

    @app.get("/api/approvals")
    async def api_approvals():
        return {"pending": ops.store.pending(), "all": ops.store.all()[:20], "language": ops.store.language,
                "reply_language": ops.lang, "style": ops.style, "switched": bool(ops.store.founder.get("switched"))}

    return app


app = create_app()
