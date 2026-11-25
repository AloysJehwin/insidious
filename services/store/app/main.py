"""Jeffy: a festive jutti D2C store on the Insidious SDK, plus the /ops cockpit.

Heavy endpoints are plain ``def`` (run in Starlette's threadpool) so CPU work saturates a 1-worker pod and queues
requests realistically. Flags are evaluated per request with identity = ``fk_sid`` cookie and trait ``cart_value``.

Catalogue reads and orders live in the database (``app.db``, via ``fk.database()``): reads use the read engine
(replica when ``read_from_replica`` is ON), order writes use the primary. Carts stay in memory.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from insidious import Insidious
from insidious.config import env as _env  # INSIDIOUS_* with legacy FESTIVEKIT_* fallback
from insidious.db import mask_url
from insidious.metrics import PAYMENT_FAILURES_TOTAL

from . import catalog
from . import ops_flags
from .db import DBUnavailable, StoreDB

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
log = logging.getLogger("store")
logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request lines for payment / guardian calls

SESSION_COOKIE = "fk_sid"
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments-mock:8001").rstrip("/")
GUARDIAN_URL = os.environ.get("GUARDIAN_URL", "http://guardian:8002").rstrip("/")
PAYMENTS_TIMEOUT_S = float(os.environ.get("PAYMENTS_TIMEOUT_S", "3"))
HIGH_VALUE_CART = 2000
POD = os.environ.get("HOSTNAME") or socket.gethostname()


def _jlog(event: str, **kw: Any) -> None:
    log.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "svc": "store-api",
                         "event": event, **kw}, default=str))


def _parse_aliases(raw: str | None) -> dict[str, str]:
    """METRICS_ROUTE_ALIASES="/api/checkout=/checkout,..." ("none" disables)."""
    if raw is None:
        raw = "/api/checkout=/checkout"
    if raw.strip().lower() in {"", "none", "off"}:
        return {}
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


fk = Insidious.from_env(
    metrics_exclude=("/metrics", "/healthz", "/ops", "/ops/state", "/ops/flags", "/ops/flags/{name}", "/api/flags",
                     "/api/experiment", "/api/db"),
    route_aliases=_parse_aliases(os.environ.get("METRICS_ROUTE_ALIASES")),
)

app = FastAPI(title="Jeffy store (Insidious demo)", docs_url="/api/docs", openapi_url="/api/openapi.json")
fk.install(app)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
sdb = StoreDB(fk.database())  # engines are lazy: nothing connects until the first query


_last_db_error_log = 0.0


def _db_unavailable(request: Request, exc: Exception) -> Response:
    """DB not ready / unreachable / pool exhausted -> 503 (counted by the SLO metrics like any 5xx)."""
    global _last_db_error_log
    now = time.monotonic()
    if now - _last_db_error_log > 10:  # rate-limited: a dead DB under load must not flood stdout
        _last_db_error_log = now
        _jlog("db_unavailable", path=request.url.path, error=f"{type(exc).__name__}: {str(exc).splitlines()[0]}"[:300]
              if str(exc) else type(exc).__name__)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "db_unavailable", "detail": type(exc).__name__}, status_code=503,
                            headers={"Retry-After": "2"})
    return HTMLResponse("<h1>We are restocking the shelves</h1><p>Please try again in a few seconds.</p>",
                        status_code=503, headers={"Retry-After": "2"})


app.add_exception_handler(DBUnavailable, _db_unavailable)
app.add_exception_handler(SQLAlchemyError, _db_unavailable)


def inr(value: Any) -> str:
    """Indian digit grouping: 149999 -> ₹1,49,999."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return f"₹{value}"
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if n < 0 else ''}₹{s}"


templates.env.filters["inr"] = inr


# ------------------------------------------------------------------------------------------- state
class Store:
    """In-memory carts and orders, bounded so a load test cannot exhaust memory."""

    def __init__(self, max_carts: int = 50000, max_orders: int = 20000) -> None:
        self.carts: collections.OrderedDict[str, dict[int, int]] = collections.OrderedDict()
        self.orders: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.max_carts, self.max_orders = max_carts, max_orders
        self.lock = threading.Lock()

    def cart(self, sid: str) -> dict[int, int]:
        with self.lock:
            return dict(self.carts.get(sid, {}))

    def add(self, sid: str, pid: int, qty: int = 1) -> dict[int, int]:
        with self.lock:
            c = self.carts.pop(sid, {})
            c[pid] = max(0, min(10, c.get(pid, 0) + qty))
            if c[pid] == 0:
                c.pop(pid)
            self.carts[sid] = c
            while len(self.carts) > self.max_carts:
                self.carts.popitem(last=False)
            return dict(c)

    def set_qty(self, sid: str, pid: int, qty: int) -> dict[int, int]:
        with self.lock:
            c = self.carts.pop(sid, {})
            if qty <= 0:
                c.pop(pid, None)
            else:
                c[pid] = min(10, qty)
            self.carts[sid] = c
            return dict(c)

    def clear(self, sid: str) -> None:
        with self.lock:
            self.carts.pop(sid, None)

    def save_order(self, order: dict) -> None:
        with self.lock:
            self.orders[order["order_id"]] = order
            while len(self.orders) > self.max_orders:
                self.orders.popitem(last=False)

    def order(self, oid: str) -> dict | None:
        with self.lock:
            return self.orders.get(oid)


store = Store()


def cart_lines(cart: dict[int, int]) -> list[dict]:
    lines = []
    for pid, qty in cart.items():
        p = catalog.get(pid)
        if p:
            lines.append({"product": p, "qty": qty, "subtotal": p.price * qty})
    return lines


def cart_total(cart: dict[int, int]) -> int:
    return sum(l["subtotal"] for l in cart_lines(cart))


class Ctx:
    """Per-request identity + flags (evaluated once, read many times)."""

    def __init__(self, request: Request) -> None:
        cookie = request.cookies.get(SESSION_COOKIE)
        self.is_new = not cookie
        self.sid = cookie or uuid.uuid4().hex
        self.refresh()

    def refresh(self) -> None:
        self.cart = store.cart(self.sid)
        self.cart_value = cart_total(self.cart)
        self.traits = {"cart_value": int(self.cart_value)}
        fk.identify(self.sid, self.traits)  # used by every @fk.degradable call in this request
        self.flags = fk.snapshot(self.sid, self.traits)

    @property
    def cart_count(self) -> int:
        return sum(self.cart.values())

    def base(self, request: Request, **extra: Any) -> dict:
        flags = self.flags
        return {
            "request": request,
            "cart_count": self.cart_count,
            "cart_value": self.cart_value,
            "high_value": self.cart_value > HIGH_VALUE_CART,
            "live": {
                "recommendations": flags.is_enabled("recommendations"),
                "hd_images": flags.is_enabled("hd_images"),
                "live_reviews": flags.is_enabled("live_reviews"),
                "checkout_flow": flags.value("checkout_flow", "full"),
                "payment_gateway": flags.value("payment_gateway", "razorpay"),
                "festive_collection": flags.is_enabled("festive_collection"),
            },
            "pod": POD,
            **extra,
        }

    def finish(self, response: Response) -> Response:
        if self.is_new:
            response.set_cookie(SESSION_COOKIE, self.sid, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return response


# ------------------------------------------------------------------------------------------- degradable features
def _cached_recs(seed_ids: list[int], k: int = 4) -> tuple[str, list[catalog.Product]]:
    return "cached", catalog.cached_top_sellers(seed_ids, None, k)


@fk.degradable("recommendations", fallback=_cached_recs)
def recommend(seed_ids: list[int], k: int = 4) -> tuple[str, list[catalog.Product]]:
    identity, _ = fk.current_identity
    candidates = sdb.recommendation_candidates()  # read engine (replica when routed); errors degrade to cached
    return "live", catalog.compute_recommendations(seed_ids, identity, k, candidates=candidates)


@fk.degradable("live_reviews", fallback=lambda pid: catalog.cached_reviews(pid))
def reviews(pid: int) -> dict:
    return catalog.live_reviews(pid)


@fk.degradable("hd_images", fallback=lambda pid: (False, catalog.product_svg(pid, False)))
def hero_image(pid: int) -> tuple[bool, str]:
    return True, catalog.product_svg(pid, True)


@fk.degradable("hd_images", fallback=lambda ids: (False, catalog.grid_svgs(ids, False)))
def grid_images(ids: list[int]) -> tuple[bool, dict[int, str]]:
    return True, catalog.grid_svgs(ids, True)


# ------------------------------------------------------------------------------------------- payments
_http = httpx.Client(
    timeout=httpx.Timeout(PAYMENTS_TIMEOUT_S, connect=1.0),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
)


def pay(gateway: str, amount: int, order_id: str) -> tuple[bool, dict]:
    if PAYMENTS_URL in {"inline", "mock"}:  # no payments-mock running (local dev)
        time.sleep(0.03)
        return True, {"ok": True, "payment_id": f"pay_inline_{uuid.uuid4().hex[:8]}", "gateway": gateway}
    try:
        r = _http.post(f"{PAYMENTS_URL}/pay", json={"gateway": gateway, "amount": amount, "order_id": order_id})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return True, data
        return False, {"status": r.status_code, **data}
    except (httpx.HTTPError, ValueError) as exc:
        return False, {"error": type(exc).__name__, "detail": str(exc)[:200]}


def _finish_order(order_id: str, status: str, payment_id: str | None) -> None:
    """Record the payment outcome. Money already moved, so a DB hiccup here is logged, not shown to the shopper."""
    try:
        sdb.mark_order(order_id, status, payment_id)
    except (SQLAlchemyError, DBUnavailable) as exc:
        _jlog("order_update_failed", order_id=order_id, status=status, error=type(exc).__name__)


def place_order(ctx: Ctx, *, autofill: bool) -> tuple[int, dict]:
    """Shared checkout for the HTML form and POST /api/checkout."""
    if not ctx.cart and autofill:
        store.add(ctx.sid, random.choice(catalog.PRODUCTS).id, 1)
    ctx.refresh()  # re-evaluate flags with the final cart_value trait
    if not ctx.cart:
        return 400, {"ok": False, "error": "empty_cart"}
    lines = cart_lines(ctx.cart)
    total = ctx.cart_value
    flow = ctx.flags.value("checkout_flow", "full")
    gateway = ctx.flags.value("payment_gateway", "razorpay")
    order_id = f"JFY-{datetime.now(timezone.utc).strftime('%y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    fk.track("checkout_started", identity=ctx.sid, properties={"checkout_flow": flow, "cart_value": total})

    fbt_source, fbt = "skipped", []
    if flow == "full":  # full flow computes "frequently bought together"; shedding recommendations speeds it up
        fbt_source, fbt = recommend(list(ctx.cart.keys()), 3)

    items = [{"product_id": l["product"].id, "name": l["product"].name, "qty": l["qty"],
              "price": l["product"].price, "subtotal": l["subtotal"]} for l in lines]
    # Write path: always the primary. If this fails we answer 503 before any money moves.
    sdb.insert_order(order_id, ctx.sid, total, gateway, flow, items, status="pending")

    ok, payment = pay(gateway, total, order_id)
    _finish_order(order_id, "paid" if ok else "failed", payment.get("payment_id") if ok else None)
    if not ok:
        PAYMENT_FAILURES_TOTAL.labels(gateway=gateway).inc()
        fk.track("payment_failed", identity=ctx.sid, properties={"checkout_flow": flow, "gateway": gateway})
        _jlog("payment_failed", gateway=gateway, order_id=order_id, detail=payment)
        return 502, {"ok": False, "error": "payment_failed", "gateway": gateway, "order_id": order_id,
                     "checkout_flow": flow, "detail": payment}

    order = {
        "order_id": order_id,
        "sid": ctx.sid,
        "items": items,
        "total": total,
        "checkout_flow": flow,
        "gateway": gateway,
        "payment_id": payment.get("payment_id"),
        "placed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eta": (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%a, %d %b"),
        "frequently_bought_together": [p.to_api() for p in fbt],
        "recommendations_source": fbt_source,
    }
    store.save_order(order)
    store.clear(ctx.sid)
    fk.track("order_placed", identity=ctx.sid,
             properties={"checkout_flow": flow, "gateway": gateway, "value": total, "order_id": order_id})
    return 200, {"ok": True, **{k: v for k, v in order.items() if k != "sid"}}


# ------------------------------------------------------------------------------------------- shop pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = Ctx(request)
    products = sdb.list_products()
    ids = [p.id for p in products]
    hd, images = grid_images(ids)
    rec_source, recs = recommend(list(ctx.cart.keys()), 4)
    hero = catalog.get(11)
    resp = templates.TemplateResponse(
        request, "home.html",
        ctx.base(request, products=products, images=images, hd=hd, recs=recs, rec_source=rec_source,
                 hero=hero, hero_svg=images[hero.id]),
    )
    return ctx.finish(resp)


@app.get("/product/{id}", response_class=HTMLResponse)
def product_page(request: Request, id: int):
    ctx = Ctx(request)
    p = sdb.get_product(id)
    if p is None:
        return ctx.finish(templates.TemplateResponse(request, "notfound.html", ctx.base(request), status_code=404))
    hd, svg = hero_image(id)
    rv = reviews(id)
    rec_source, recs = recommend([id], 4)
    rec_imgs = catalog.grid_svgs([r.id for r in recs], False)
    resp = templates.TemplateResponse(
        request, "product.html",
        ctx.base(request, p=p, svg=svg, hd=hd, reviews=rv, recs=recs, rec_source=rec_source, rec_imgs=rec_imgs),
    )
    return ctx.finish(resp)


@app.post("/cart/add")
def cart_add(request: Request, product_id: int = Form(...), qty: int = Form(1), next: str = Form("/cart")):
    ctx = Ctx(request)
    if catalog.get(product_id):
        store.add(ctx.sid, product_id, max(1, qty))
    target = next if next.startswith("/") and not next.startswith("//") else "/cart"
    return ctx.finish(RedirectResponse(target, status_code=303))


@app.post("/cart/update")
def cart_update(request: Request, product_id: int = Form(...), qty: int = Form(...)):
    ctx = Ctx(request)
    store.set_qty(ctx.sid, product_id, qty)
    return ctx.finish(RedirectResponse("/cart", status_code=303))


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    ctx = Ctx(request)
    lines = cart_lines(ctx.cart)
    imgs = catalog.grid_svgs([l["product"].id for l in lines], False)
    resp = templates.TemplateResponse(
        request, "cart.html",
        ctx.base(request, lines=lines, imgs=imgs, total=ctx.cart_value,
                 to_royal=max(0, HIGH_VALUE_CART + 1 - ctx.cart_value)),
    )
    return ctx.finish(resp)


@app.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request):
    ctx = Ctx(request)
    if not ctx.cart:
        return ctx.finish(RedirectResponse("/cart", status_code=303))
    flow = ctx.flags.value("checkout_flow", "full")
    gateway = ctx.flags.value("payment_gateway", "razorpay")
    lines = cart_lines(ctx.cart)
    fbt_source, fbt = ("skipped", [])
    if flow == "full":
        fbt_source, fbt = recommend(list(ctx.cart.keys()), 3)
    imgs = catalog.grid_svgs([l["product"].id for l in lines] + [p.id for p in fbt], False)
    fk.track("checkout_viewed", identity=ctx.sid, properties={"checkout_flow": flow})
    resp = templates.TemplateResponse(
        request, "checkout.html",
        ctx.base(request, flow=flow, gateway=gateway, lines=lines, imgs=imgs, total=ctx.cart_value, fbt=fbt,
                 fbt_source=fbt_source, error=None),
    )
    return ctx.finish(resp)


@app.post("/checkout", response_class=HTMLResponse)
def checkout_submit(request: Request):
    ctx = Ctx(request)
    status, result = place_order(ctx, autofill=False)
    if status == 200:
        return ctx.finish(RedirectResponse(f"/order/{result['order_id']}", status_code=303))
    if status == 400:
        return ctx.finish(RedirectResponse("/cart", status_code=303))
    lines = cart_lines(ctx.cart)
    imgs = catalog.grid_svgs([l["product"].id for l in lines], False)
    resp = templates.TemplateResponse(
        request, "checkout.html",
        ctx.base(request, flow=result.get("checkout_flow", "full"), gateway=result.get("gateway"), lines=lines,
                 imgs=imgs, total=ctx.cart_value, fbt=[], fbt_source="skipped", error=result),
        status_code=502,
    )
    return ctx.finish(resp)


def _order_from_db(order_id: str) -> dict | None:
    """Orders placed on another pod (or before a restart) are rebuilt from the orders table."""
    row = sdb.get_order(order_id)
    if row is None:
        return None
    try:
        placed = datetime.fromisoformat(str(row["placed_at"]).replace("Z", "+00:00"))
    except ValueError:
        placed = datetime.now(timezone.utc)
    return {**row, "eta": (placed + timedelta(days=3)).strftime("%a, %d %b"), "frequently_bought_together": [],
            "recommendations_source": "skipped"}


@app.get("/order/{order_id}", response_class=HTMLResponse)
def order_page(request: Request, order_id: str):
    ctx = Ctx(request)
    order = store.order(order_id) or _order_from_db(order_id)
    if order is None:
        return ctx.finish(templates.TemplateResponse(request, "notfound.html", ctx.base(request), status_code=404))
    imgs = catalog.grid_svgs([i["product_id"] for i in order["items"]], False)
    return ctx.finish(templates.TemplateResponse(request, "order.html", ctx.base(request, order=order, imgs=imgs)))


@app.get("/img/{id}.svg")
def product_image(request: Request, id: int):
    ctx = Ctx(request)
    if catalog.get(id) is None:
        return Response(status_code=404)
    _, svg = hero_image(id)
    return ctx.finish(Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"}))


# ------------------------------------------------------------------------------------------- JSON API (k6)
class CartItem(BaseModel):
    product_id: int
    qty: int = 1


@app.get("/api/products")
def api_products(request: Request):
    ctx = Ctx(request)
    return ctx.finish(JSONResponse({"products": [p.to_api() for p in sdb.list_products()]}))


@app.get("/api/products/{id}")
def api_product(request: Request, id: int):
    ctx = Ctx(request)
    p = sdb.get_product(id)
    if p is None:
        return ctx.finish(JSONResponse({"error": "not_found"}, status_code=404))
    hd, svg = hero_image(id)
    rv = reviews(id)
    return ctx.finish(JSONResponse({
        **p.to_api(), "description": p.description, "sizes": list(p.sizes),
        "image_quality": "hd" if hd else "standard", "image_bytes": len(svg),
        "reviews": rv,
    }))


@app.get("/api/recommendations")
def api_recommendations(request: Request, pid: Optional[int] = None):
    ctx = Ctx(request)
    sdb.require()  # 503 until the database is ready (afterwards DB errors degrade to cached recommendations)
    seeds = [pid] if pid else list(ctx.cart.keys())
    source, recs = recommend(seeds, 4)
    return ctx.finish(JSONResponse({"source": source, "items": [p.to_api() for p in recs]}))


@app.post("/api/cart")
def api_cart(request: Request, item: CartItem):
    ctx = Ctx(request)
    if catalog.get(item.product_id) is None:
        return ctx.finish(JSONResponse({"error": "unknown_product"}, status_code=404))
    cart = store.add(ctx.sid, item.product_id, max(1, item.qty))
    lines = cart_lines(cart)
    return ctx.finish(JSONResponse({
        "items": [{"product_id": l["product"].id, "qty": l["qty"], "subtotal": l["subtotal"]} for l in lines],
        "cart_value": sum(l["subtotal"] for l in lines),
    }))


@app.get("/api/cart")
def api_cart_get(request: Request):
    ctx = Ctx(request)
    lines = cart_lines(ctx.cart)
    return ctx.finish(JSONResponse({
        "items": [{"product_id": l["product"].id, "qty": l["qty"], "subtotal": l["subtotal"]} for l in lines],
        "cart_value": ctx.cart_value,
    }))


@app.post("/api/checkout")
def api_checkout(request: Request, body: Optional[dict] = Body(default=None)):
    ctx = Ctx(request)
    status, result = place_order(ctx, autofill=True)
    return ctx.finish(JSONResponse(result, status_code=status))


@app.get("/api/flags")
def api_flags():
    return fk.env_states()


@app.get("/api/experiment")
def api_experiment():
    return {"flag": "checkout_flow", "metric": "order_placed", "variants": fk.events.experiment_summary(
        exposure_event="checkout_started", conversion_event="order_placed")}


@app.get("/api/db")
async def api_db():
    """Pool numbers for the primary (and replica when reads are routed to it). Async: answers even under load."""
    return sdb.info()


def _redacted(overrides: dict) -> dict:
    """db_read_url may carry a password: never echo or log it in clear."""
    out = dict(overrides)
    if out.get("db_read_url"):
        out["db_read_url"] = mask_url(out["db_read_url"])
    return out


def _dev_guard() -> Optional[JSONResponse]:
    if not fk.dev_mode:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return None


@app.get("/api/dev/flags")
def dev_flags_get():
    if (r := _dev_guard()) is not None:
        return r
    return {"overrides": _redacted(fk.flags.dev_overrides), "flags": fk.env_states()}


@app.post("/api/dev/flags")
def dev_flags_set(body: dict = Body(...)):
    if (r := _dev_guard()) is not None:
        return r
    known = set(fk.flags.features)
    unknown = [k for k in body if k not in known]
    if unknown:
        return JSONResponse({"error": "unknown_flags", "flags": unknown}, status_code=400)
    overrides = _redacted(fk.set_dev_overrides(body))
    _jlog("dev_flags_set", overrides=overrides)
    return {"overrides": overrides, "flags": fk.env_states()}


@app.delete("/api/dev/flags")
def dev_flags_clear():
    if (r := _dev_guard()) is not None:
        return r
    fk.set_dev_overrides({}, replace=True)
    _jlog("dev_flags_cleared")
    return {"overrides": {}, "flags": fk.env_states()}


@app.get("/healthz")
async def healthz():
    # async so the probe never queues behind CPU-bound work in the threadpool
    return {"ok": True}


@app.on_event("startup")
async def _limit_threadpool() -> None:
    # Fewer worker threads = less GIL contention, so the event loop (healthz, /metrics, /ops)
    # stays responsive while CPU-heavy endpoints queue and their latency climbs realistically.
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = int(os.environ.get("STORE_THREADS", "8"))


@app.on_event("startup")
async def _start_db() -> None:
    # Schema + seed in the background with retry/backoff: the pod starts (and /healthz answers) even when the
    # database is not reachable yet; DB endpoints return 503 until it is.
    sdb.start()


@app.on_event("shutdown")
async def _stop_db() -> None:
    sdb.close()


# ------------------------------------------------------------------------------------------- /ops cockpit
_guardian = httpx.AsyncClient(timeout=httpx.Timeout(1.5, connect=0.8))


@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request):
    return templates.TemplateResponse(request, "ops.html", {"request": request, "pod": POD,
                                                            "guardian_url": GUARDIAN_URL})


@app.get("/ops/state")
async def ops_state():
    store_info = {"flags": fk.env_states(), "experiment": fk.events.experiment_summary(), "pod": POD,
                  "dev_overrides": _redacted(fk.flags.dev_overrides) if fk.dev_mode else {}}
    try:
        store_info["db"] = sdb.info()
    except Exception as exc:  # noqa: BLE001
        store_info["db"] = {"error": type(exc).__name__}
    try:
        r = await _guardian.get(f"{GUARDIAN_URL}/api/state")
        r.raise_for_status()
        state = r.json()
        return {"guardian_online": True, "guardian_url": GUARDIAN_URL, "state": state, "store": store_info}
    except Exception as exc:  # noqa: BLE001
        return {"guardian_online": False, "guardian_url": GUARDIAN_URL, "error": f"{type(exc).__name__}: {exc}"[:300],
                "state": None, "store": store_info}


# ------------------------------------------------------------------------------------------- /ops flag toggles
# GET lists environment-level flags straight from the Flagsmith Admin API (cached 3 s); POST patches one
# environment featurestate. The store's own flag reads keep using local evaluation, so a toggle reaches every
# pod within FLAGSMITH_REFRESH_SECONDS.
_flagsmith_http = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))
OPS_FLAGS_CACHE_S = 3.0
_ops_flags_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_ops_flags_lock = asyncio.Lock()


def _ops_admin() -> ops_flags.FlagsmithAdmin:
    return ops_flags.FlagsmithAdmin(_flagsmith_http)


def _refresh_seconds() -> float:
    try:
        return float(os.environ.get("FLAGSMITH_REFRESH_SECONDS", "5") or 5)
    except ValueError:
        return 5.0


def _ops_flags_invalidate() -> None:
    _ops_flags_cache.update(ts=0.0, data=None)


def _sdk_flag_rows() -> list[dict]:
    """Read-only fallback when the Admin API is not configured: what this pod evaluates right now."""
    states = fk.flags.evaluate(None, None)
    rows = []
    for name, spec in fk.flags.features.items():
        st = states.get(name)
        enabled, value = (bool(st.enabled), st.value) if st is not None else (False, spec.get("default_value"))
        kind = ops_flags.kind_of(spec.get("type"), value)
        rows.append(ops_flags.describe(name, enabled, value if kind == "config" else None,
                                       spec.get("description", ""), kind))
    return ops_flags.sort_rows(rows)


async def _load_ops_flags() -> dict:
    admin = _ops_admin()
    writable = admin.configured and bool(_env("INSIDIOUS_OPS_PASSCODE"))
    base = {"groups": ops_flags.GROUP_ORDER, "refresh_seconds": _refresh_seconds(), "writable": writable}
    if not admin.configured:
        return {**base, "source": "sdk", "flags": _sdk_flag_rows(),
                "note": "Flagsmith Admin API not configured (FLAGSMITH_ORG_API_KEY / FLAGSMITH_ENVIRONMENT_KEY); "
                        "showing this pod's evaluated flags, read-only."}
    features, states = await asyncio.gather(admin.list_features(), admin.environment_states())
    return {**base, "source": "flagsmith", "flags": ops_flags.merge(features, states, fk.flags.features)}


@app.get("/ops/flags")
async def ops_flags_list():
    now = time.monotonic()
    cached = _ops_flags_cache["data"]
    if cached is not None and now - _ops_flags_cache["ts"] < OPS_FLAGS_CACHE_S:
        return cached
    async with _ops_flags_lock:
        cached = _ops_flags_cache["data"]
        if cached is not None and time.monotonic() - _ops_flags_cache["ts"] < OPS_FLAGS_CACHE_S:
            return cached
        try:
            data = await _load_ops_flags()
        except ops_flags.FlagsmithError as exc:
            _jlog("ops_flags_list_failed", error=str(exc)[:300])
            return JSONResponse({"error": "flagsmith_unavailable", "detail": str(exc)[:300]}, status_code=502)
        _ops_flags_cache.update(ts=time.monotonic(), data=data)
        return data


@app.post("/ops/flags/{name}")
async def ops_flags_set(name: str, request: Request, body: Any = Body(default=None)):
    passcode = _env("INSIDIOUS_OPS_PASSCODE", "")
    if not passcode:
        return JSONResponse({"error": "ops_passcode_not_configured",
                             "detail": "set INSIDIOUS_OPS_PASSCODE to allow flag changes from the cockpit"},
                            status_code=503)
    given = request.headers.get("x-ops-passcode", "")
    if not given or not hmac.compare_digest(given.encode(), passcode.encode()):
        return JSONResponse({"error": "bad_passcode"}, status_code=401)

    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request", "detail": 'expected {"enabled": bool} and/or {"value": str}'},
                            status_code=400)
    enabled, value = body.get("enabled"), body.get("value")
    if enabled is not None and not isinstance(enabled, bool):
        return JSONResponse({"error": "bad_request", "detail": "enabled must be true or false"}, status_code=400)
    if value is not None and not isinstance(value, str):
        return JSONResponse({"error": "bad_request", "detail": "value must be a string"}, status_code=400)
    if enabled is None and value is None:
        return JSONResponse({"error": "bad_request", "detail": "nothing to change"}, status_code=400)
    if value is not None:
        if ops_flags.is_sensitive(name):
            _jlog("ops_flag_value_rejected", flag=name)
            return JSONResponse({"error": "value_edit_forbidden",
                                 "detail": f"{name} cannot be edited from the dashboard"}, status_code=403)
        opts = ops_flags.VALUE_OPTIONS.get(name)
        if opts is not None and value not in opts:
            return JSONResponse({"error": "bad_value", "detail": f"{name} must be one of {', '.join(opts)}"},
                                status_code=400)
        if len(value) > 2000:
            return JSONResponse({"error": "bad_value", "detail": "value too long"}, status_code=400)

    admin = _ops_admin()
    if not admin.configured:
        return JSONResponse({"error": "flagsmith_admin_not_configured",
                             "detail": "set FLAGSMITH_ORG_API_KEY and FLAGSMITH_ENVIRONMENT_KEY"}, status_code=503)

    patch: dict[str, Any] = {}
    if enabled is not None:
        patch["enabled"] = enabled
    if value is not None:
        patch["feature_state_value"] = value
    try:
        fs = await admin.set_state(name, patch)
    except ops_flags.FlagsmithError as exc:
        _jlog("ops_flag_set_failed", flag=name, error=str(exc)[:300])
        status = 404 if exc.status == 404 else 502
        return JSONResponse({"error": "unknown_flag" if status == 404 else "flagsmith_error",
                             "detail": str(exc)[:300]}, status_code=status)
    _ops_flags_invalidate()

    new_enabled = fs.get("enabled", enabled) if isinstance(fs.get("enabled"), bool) else enabled
    new_value = ops_flags.raw_value(fs) if "feature_state_value" in fs else value
    spec = fk.flags.features.get(name, {})
    row = ops_flags.describe(name, bool(new_enabled), new_value, spec.get("description", ""),
                             ops_flags.kind_of(spec.get("type"), new_value))
    if value is not None:
        text = f"{name} set to {value} from the dashboard"
    else:
        text = f"{name} turned {'ON' if enabled else 'OFF'} from the dashboard"
    _jlog("ops_flag_set", flag=name, enabled=enabled, value=ops_flags.mask_value(name, value))
    feed_posted = False
    try:
        r = await _guardian.post(f"{GUARDIAN_URL}/api/feed", json={"source": "dashboard", "text": text})
        feed_posted = r.status_code < 400
    except Exception:  # noqa: BLE001  best effort: the flag already changed
        pass
    return {"ok": True, "flag": row, "message": text, "refresh_seconds": _refresh_seconds(),
            "feed_posted": feed_posted}
