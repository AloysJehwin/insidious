"""guardian HTTP API (port 8002). Run: uvicorn app.main:app --host 0.0.0.0 --port 8002"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from .config import Settings, env_flag
from .engine import Guardian
from .log import log


class ArmRequest(BaseModel):
    armed: bool = True


class FeedRequest(BaseModel):
    source: str = "ops-bot"
    text: str


class SampleRequest(BaseModel):
    label: str = "pass1"
    target_rps: float | None = None
    window: str | None = None  # optional PromQL rate window override, e.g. "20s"


def create_app(guardian: Guardian | None = None, start_loop: bool | None = None) -> FastAPI:
    start_loop = env_flag("GUARDIAN_LOOP", "1") if start_loop is None else start_loop

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = None
        g = guardian
        if g is None:
            settings = Settings.from_env()
            client = httpx.AsyncClient(timeout=settings.http_timeout)
            g = Guardian(settings, client)
        app.state.guardian = g
        task = asyncio.create_task(g.run_forever()) if start_loop else None
        try:
            yield
        finally:
            g.stop()
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await g.replica_reader.aclose()
            if client:
                await client.aclose()

    app = FastAPI(title="insidious guardian", lifespan=lifespan)

    def g() -> Guardian:
        return app.state.guardian

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return g().snapshot()

    @app.post("/api/arm")
    async def arm(req: ArmRequest):
        g().armed = req.armed
        g().add_feed("guardian", "Guard armed: SLO breaches will page NudgeBee" if req.armed
                     else "Guard disarmed: breaches are shown but not sent")
        log("armed", armed=req.armed)
        return {"armed": g().armed}

    @app.post("/api/feed")
    async def feed(req: FeedRequest):
        g().add_feed(req.source, req.text)
        return {"ok": True}

    @app.post("/api/rehearse/sample")
    async def rehearse_sample(req: SampleRequest):
        return await g().rehearse_sample(req.label, req.target_rps, req.window)

    return app


app = create_app()
