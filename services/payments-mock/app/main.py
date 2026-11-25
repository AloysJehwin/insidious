"""payments-mock: fake razorpay / cashfree gateways with latency + error knobs and a runtime chaos API.

Env knobs (per gateway, upper-case name): PAY_RAZORPAY_LATENCY_MS (default 80), PAY_RAZORPAY_ERROR_RATE (0.0),
PAY_CASHFREE_LATENCY_MS (default 120), PAY_CASHFREE_ERROR_RATE (0.0), PAY_JITTER_MS (default 20),
PAY_GATEWAYS (comma list, default "razorpay,cashfree").
No real money moves anywhere: this service only sleeps and returns JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

DEFAULT_LATENCY = {"razorpay": 80, "cashfree": 120}

PAYMENTS = Counter("payments_mock_requests_total", "Mock payments", ["gateway", "result"])
PAY_LATENCY = Histogram("payments_mock_latency_seconds", "Mock payment latency", ["gateway"])


def _log(event: str, **kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "svc": "payments-mock",
                      "event": event, **kw}), flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def initial_chaos() -> dict[str, dict[str, float]]:
    names = [g.strip().lower() for g in os.environ.get("PAY_GATEWAYS", "razorpay,cashfree").split(",") if g.strip()]
    out = {}
    for g in names:
        out[g] = {
            "latency_ms": _env_float(f"PAY_{g.upper()}_LATENCY_MS", DEFAULT_LATENCY.get(g, 100)),
            "error_rate": max(0.0, min(1.0, _env_float(f"PAY_{g.upper()}_ERROR_RATE", 0.0))),
        }
    return out


class PayRequest(BaseModel):
    gateway: str = "razorpay"
    amount: float = 0
    order_id: str = Field(default_factory=lambda: f"ord_{uuid.uuid4().hex[:10]}")


class ChaosRequest(BaseModel):
    gateway: str
    latency_ms: Optional[float] = None
    error_rate: Optional[float] = None


app = FastAPI(title="payments-mock")
app.state.chaos = initial_chaos()
app.state.jitter_ms = _env_float("PAY_JITTER_MS", 20)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/pay")
async def pay(req: PayRequest):
    gw = req.gateway.lower().strip()
    knobs = app.state.chaos.get(gw)
    if knobs is None:
        PAYMENTS.labels(gateway=gw or "unknown", result="unknown_gateway").inc()
        return JSONResponse({"ok": False, "error": "unknown_gateway", "gateway": gw}, status_code=400)
    jitter = random.uniform(0, app.state.jitter_ms) if app.state.jitter_ms > 0 else 0
    delay_ms = max(0.0, knobs["latency_ms"] + jitter)
    start = time.perf_counter()
    await asyncio.sleep(delay_ms / 1000.0)
    PAY_LATENCY.labels(gateway=gw).observe(time.perf_counter() - start)
    if random.random() < knobs["error_rate"]:
        PAYMENTS.labels(gateway=gw, result="error").inc()
        return JSONResponse(
            {"ok": False, "error": "gateway_error", "gateway": gw, "order_id": req.order_id,
             "latency_ms": round(delay_ms, 1)},
            status_code=502,
        )
    PAYMENTS.labels(gateway=gw, result="ok").inc()
    return {
        "ok": True,
        "payment_id": f"pay_{uuid.uuid4().hex[:14]}",
        "gateway": gw,
        "amount": req.amount,
        "order_id": req.order_id,
        "latency_ms": round(delay_ms, 1),
        "mock": True,
    }


@app.get("/chaos")
async def get_chaos():
    return {"gateways": app.state.chaos, "jitter_ms": app.state.jitter_ms}


@app.post("/chaos")
async def set_chaos(req: ChaosRequest):
    gw = req.gateway.lower().strip()
    knobs = app.state.chaos.setdefault(gw, {"latency_ms": DEFAULT_LATENCY.get(gw, 100), "error_rate": 0.0})
    if req.latency_ms is not None:
        knobs["latency_ms"] = max(0.0, float(req.latency_ms))
    if req.error_rate is not None:
        knobs["error_rate"] = max(0.0, min(1.0, float(req.error_rate)))
    _log("chaos_set", gateway=gw, **knobs)
    return {"gateway": gw, **knobs}


@app.post("/chaos/reset")
async def reset_chaos():
    app.state.chaos = initial_chaos()
    _log("chaos_reset")
    return {"gateways": app.state.chaos}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
