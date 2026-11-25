"""agent HTTP API (port 8004). Run: uvicorn app.main:app --host 0.0.0.0 --port 8004"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .log import log
from .models import AskRequest, DecisionRequest, IncidentRequest
from .service import AgentService, HTTPProblem


def create_app(service: AgentService | None = None, settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = None
        svc = service
        if svc is None:
            s = settings or Settings.from_env()
            client = httpx.AsyncClient(timeout=s.http_timeout)
            svc = AgentService(s, client)
            log("agent_started", mock=s.mock, dry_run=s.dry_run, provider=s.provider, model=s.model,
                k8s=svc.k8s.available, rds=bool(s.rds_id), flagsmith_admin=svc.flagsmith.configured,
                guardrails=s.guardrails)
            svc.spawn(svc.runtime.get())  # warm up Flagsmith local evaluation before the first incident
        app.state.svc = svc
        try:
            yield
        finally:
            if service is None:
                await svc.aclose()
            if client:
                await client.aclose()

    app = FastAPI(title="insidious agent", lifespan=lifespan)

    def svc() -> AgentService:
        return app.state.svc

    def check_token(request: Request) -> None:
        want = svc().s.internal_token
        # X-FestiveKit-Token is the pre-rename header; still accepted (NudgeBee workflows may send it).
        got = request.headers.get("x-insidious-token") or request.headers.get("x-festivekit-token")
        if not want:
            log("internal_token_missing", note="INSIDIOUS_INTERNAL_TOKEN is not set; rejecting")
            raise HTTPException(401, "internal token not configured")
        if not got or not hmac.compare_digest(got, want):
            raise HTTPException(401, "bad X-Insidious-Token")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/api/incident", status_code=202)
    async def incident(req: IncidentRequest, request: Request):
        check_token(request)
        return JSONResponse(await svc().submit_incident(req), status_code=202)

    @app.get("/api/plans")
    async def plans(limit: int = 20):
        return [p.model_dump() for p in svc().store.all()[: max(1, min(limit, 200))]]

    @app.get("/api/plans/{plan_id}")
    async def plan(plan_id: str):
        p = svc().store.get(plan_id)
        if p is None:
            raise HTTPException(404, f"plan {plan_id} not found")
        return p.model_dump()

    @app.post("/api/plans/{plan_id}/decision")
    async def decision(plan_id: str, req: DecisionRequest, request: Request):
        check_token(request)
        try:
            return await svc().decide(plan_id, req)
        except HTTPProblem as e:
            raise HTTPException(e.status, e.detail) from None

    @app.post("/api/ask")
    async def ask(req: AskRequest, request: Request):
        check_token(request)
        return await svc().ask(req)

    return app


app = create_app()
