"""Store database: products + orders on the Insidious flag-routed database (SQLAlchemy 2 Core).

* Primary = DATABASE_URL (SQLite file fallback when unset); pool size from Flagsmith remote config ``db_pool``.
* Reads (catalogue, product detail, recommendation candidates) use ``db.read()`` = the replica while flag
  ``read_from_replica`` is ON and ``db_read_url`` is set; writes (orders) always use the primary.
* Schema + seed (from catalog.PRODUCTS) are created on startup if missing (idempotent, safe across pods).
  If the database is unreachable a background thread retries with backoff; until then DB endpoints raise
  ``DBUnavailable`` (the app maps it to 503) while /healthz stays 200.
* ``DB_QUERY_WORK_MS`` (default 5) adds a realistic per-query cost: ``SELECT pg_sleep()`` on Postgres,
  ``time.sleep`` on SQLite, so pool saturation shows up under demo load.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TypeVar

from sqlalchemy import (Column, DateTime, Float, Integer, MetaData, String, Table, Text, insert, select, text,
                        update)
from sqlalchemy.exc import DBAPIError

from .catalog import PRODUCTS, Product

log = logging.getLogger("store.db")
T = TypeVar("T")

metadata = MetaData()

products = Table(
    "products", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("name", String(200), nullable=False),
    Column("tagline", String(300), nullable=False, default=""),
    Column("price", Integer, nullable=False),
    Column("mrp", Integer, nullable=False),
    Column("category", String(50), nullable=False),
    Column("rating", Float, nullable=False, default=0),
    Column("sales", Integer, nullable=False, default=0),
    Column("badge", String(50), nullable=False, default=""),
    Column("description", Text, nullable=False, default=""),
    Column("attrs", Text, nullable=False, default="{}"),  # JSON: colours, motif, taste vector, sizes
)

orders = Table(
    "orders", metadata,
    Column("id", String(40), primary_key=True),
    Column("session_id", String(64), nullable=False, index=True),
    Column("total", Integer, nullable=False),
    Column("gateway", String(32), nullable=False),
    Column("flow", String(16), nullable=False),
    Column("status", String(16), nullable=False, default="pending"),
    Column("payment_id", String(80)),
    Column("items", Text, nullable=False, default="[]"),  # JSON line items
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class DBUnavailable(RuntimeError):
    """The database is not reachable / not initialised yet (mapped to HTTP 503)."""


def _jlog(event: str, level: int = logging.INFO, **kw: Any) -> None:
    log.log(level, json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "svc": "store-api",
                               "event": event, **kw}, default=str))


def product_to_row(p: Product) -> dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "tagline": p.tagline, "price": p.price, "mrp": p.mrp, "category": p.category,
        "rating": p.rating, "sales": p.sales, "badge": p.badge, "description": p.description,
        "attrs": json.dumps({"primary": p.primary, "secondary": p.secondary, "accent": p.accent, "bg": p.bg,
                             "motif": p.motif, "vec": list(p.vec), "sizes": list(p.sizes)}),
    }


def row_to_product(r: Any) -> Product:
    a = json.loads(r["attrs"] or "{}")
    return Product(
        id=int(r["id"]), name=r["name"], tagline=r["tagline"] or "", price=int(r["price"]), mrp=int(r["mrp"]),
        category=r["category"], primary=a.get("primary", "#7a1035"), secondary=a.get("secondary", "#d4a537"),
        accent=a.get("accent", "#f6d77a"), bg=a.get("bg", "#fbe9ec"), motif=a.get("motif", "dots"),
        vec=tuple(float(x) for x in a.get("vec", [0.0] * 9)), rating=float(r["rating"] or 0),
        sales=int(r["sales"] or 0), badge=r["badge"] or "", description=r["description"] or "",
        sizes=tuple(int(x) for x in a.get("sizes", (36, 37, 38, 39, 40, 41))),
    )


def _work_ms_from_env() -> float:
    try:
        return max(0.0, float(os.environ.get("DB_QUERY_WORK_MS", "5")))
    except ValueError:
        return 5.0


class StoreDB:
    def __init__(self, database: Any, seed: Iterable[Product] = PRODUCTS, work_ms: float | None = None,
                 sync_retry_s: float = 2.0) -> None:
        self.db = database
        self.seed = list(seed)
        self.work_ms = _work_ms_from_env() if work_ms is None else work_ms
        self.ready = threading.Event()
        self.last_error: str | None = None
        self.attempts = 0
        self.sync_retry_s = sync_retry_s
        self._init_lock = threading.Lock()
        self._next_sync_attempt = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_err_log = 0.0

    # ------------------------------------------------------------------ init / readiness
    def init_schema(self) -> int:
        """Create tables if missing and insert any missing seed products. Returns the number inserted."""
        eng = self.db.primary()
        metadata.create_all(eng)  # checkfirst: idempotent
        with eng.begin() as conn:
            existing = set(conn.execute(select(products.c.id)).scalars())
            missing = [product_to_row(p) for p in self.seed if p.id not in existing]
            if missing:
                conn.execute(insert(products), missing)
        return len(missing)

    def try_init(self) -> bool:
        if self.ready.is_set():
            return True
        if not self._init_lock.acquire(blocking=False):  # someone else is already trying
            return False
        try:
            if self.ready.is_set():
                return True
            self.attempts += 1
            try:
                seeded = self.init_schema()
            except Exception as exc:  # noqa: BLE001 - unreachable DB, races between pods, bad URL...
                self.last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:300]
                _jlog("db_init_failed", logging.WARNING, attempt=self.attempts, error=self.last_error)
                return False
            self.last_error = None
            self.ready.set()
            _jlog("db_ready", attempt=self.attempts, seeded=seeded, reads_from=self.db.reads_from,
                  dialect=self.db.primary().dialect.name, query_work_ms=self.work_ms)
            return True
        finally:
            self._init_lock.release()

    def start(self, initial_backoff_s: float = 0.5, max_backoff_s: float = 15.0) -> None:
        """Initialise in the background, retrying with exponential backoff until the DB answers."""
        if self.ready.is_set() or (self._thread and self._thread.is_alive()):
            return

        def run() -> None:
            delay = initial_backoff_s
            while not self._stop.is_set() and not self.try_init():
                self._stop.wait(delay)
                delay = min(max_backoff_s, delay * 2)

        self._thread = threading.Thread(target=run, name="store-db-init", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def require(self) -> None:
        """Raise DBUnavailable unless initialised. Tries a synchronous init at most every ``sync_retry_s``."""
        if self.ready.is_set():
            return
        now = time.monotonic()
        if now >= self._next_sync_attempt:
            self._next_sync_attempt = now + self.sync_retry_s
            if self.try_init():
                return
        raise DBUnavailable(self.last_error or "database not ready yet")

    # ------------------------------------------------------------------ helpers
    def _work(self, conn: Any) -> None:
        ms = self.work_ms
        if ms <= 0:
            return
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_sleep(:s)"), {"s": ms / 1000.0})
        else:
            time.sleep(ms / 1000.0)

    def _read(self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(conn)`` on the read engine; if the replica cannot be reached, retry once on the primary."""
        self.require()
        eng = self.db.read()
        try:
            with eng.connect() as conn:
                return fn(conn)
        except DBAPIError:
            primary = self.db.primary()
            if eng is primary:
                raise
            now = time.monotonic()
            if now - self._last_err_log > 10:
                self._last_err_log = now
                _jlog("db_replica_read_failed", logging.WARNING, fallback="primary")
            with primary.connect() as conn:
                return fn(conn)

    # ------------------------------------------------------------------ catalogue (read engine)
    def list_products(self) -> list[Product]:
        def q(conn: Any) -> list[Product]:
            self._work(conn)
            rows = conn.execute(select(products).order_by(products.c.id)).mappings().all()
            return [row_to_product(r) for r in rows]

        return self._read(q)

    def get_product(self, pid: int) -> Product | None:
        def q(conn: Any) -> Product | None:
            self._work(conn)
            row = conn.execute(select(products).where(products.c.id == pid)).mappings().first()
            return row_to_product(row) if row else None

        return self._read(q)

    def recommendation_candidates(self) -> list[Product]:
        return self.list_products()

    # ------------------------------------------------------------------ orders (primary)
    def insert_order(self, order_id: str, session_id: str, total: int, gateway: str, flow: str,
                     items: list[dict], status: str = "pending") -> None:
        self.require()
        with self.db.write().begin() as conn:
            self._work(conn)
            conn.execute(insert(orders).values(
                id=order_id, session_id=session_id, total=int(total), gateway=gateway, flow=flow, status=status,
                items=json.dumps(items), created_at=datetime.now(timezone.utc)))

    def mark_order(self, order_id: str, status: str, payment_id: str | None = None) -> None:
        self.require()
        with self.db.write().begin() as conn:
            self._work(conn)
            conn.execute(update(orders).where(orders.c.id == order_id).values(status=status, payment_id=payment_id))

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Read an order back from the primary (read-your-writes: a replica may lag)."""
        self.require()
        with self.db.write().connect() as conn:
            self._work(conn)
            row = conn.execute(select(orders).where(orders.c.id == order_id)).mappings().first()
        if not row:
            return None
        created = row["created_at"]
        return {
            "order_id": row["id"], "sid": row["session_id"], "total": int(row["total"]), "gateway": row["gateway"],
            "checkout_flow": row["flow"], "status": row["status"], "payment_id": row["payment_id"],
            "items": json.loads(row["items"] or "[]"),
            "placed_at": created.isoformat(timespec="seconds") if hasattr(created, "isoformat") else str(created),
        }

    # ------------------------------------------------------------------ status
    def info(self) -> dict[str, Any]:
        st = self.db.stats()
        return {
            "primary": st["primary"],
            "replica": st["replica"],
            "read_from_replica": st["read_from_replica"],
            "reads_from": st["reads_from"],
            "ready": self.ready.is_set(),
            "last_error": self.last_error,
            "query_work_ms": self.work_ms,
            "rebuilds": st["rebuilds"],
        }

    def close(self) -> None:
        self.stop()
        self.db.dispose()
