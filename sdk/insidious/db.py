"""Database helpers: SQLAlchemy pool/query metrics and flag-routed primary/replica engines.

    from insidious import Insidious
    from sqlalchemy import text

    fk = Insidious.from_env()
    db = fk.database()                    # primary from DATABASE_URL (SQLite file fallback when unset)
    with db.read().connect() as conn:     # replica when flag read_from_replica is ON and db_read_url is set
        conn.execute(text("select 1"))
    with db.write().begin() as conn:      # always the primary
        ...

``instrument_engine(engine, db="primary")`` works on any SQLAlchemy engine and emits:

* ``insidious_db_pool_size{db}``           max connections (pool_size + max_overflow)
* ``insidious_db_pool_in_use{db}``         connections checked out right now
* ``insidious_db_pool_wait_seconds{db}``   histogram: asking the pool for a connection -> getting one
* ``insidious_db_query_seconds{db,op}``    histogram: statement time, op = select | insert | update | other

Pool sizing comes from Flagsmith remote config ``db_pool`` (JSON ``{"pool_size":5,"max_overflow":0,"timeout_s":2}``);
the primary engine is rebuilt (old one disposed) when it changes, checked at most every ``check_interval_s``.

SQLAlchemy is an optional dependency: ``pip install "insidious[db]"``. This module imports it lazily so
``import insidious`` keeps working without it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import weakref
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from .config import env as _env
from .metrics import DB_POOL_IN_USE, DB_POOL_SIZE, DB_POOL_WAIT_SECONDS, DB_QUERY_SECONDS

log = logging.getLogger("insidious.db")

READ_FLAG = "read_from_replica"
READ_URL_CONFIG = "db_read_url"
POOL_CONFIG = "db_pool"

_in_use: dict[str, int] = defaultdict(int)
_in_use_lock = threading.Lock()
_instrumented: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def _jlog(level: int, event: str, **kw: Any) -> None:
    log.log(level, json.dumps({"svc": "insidious", "event": event, **kw}, default=str))


def _require_sqlalchemy() -> None:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError('insidious.db needs SQLAlchemy: pip install "insidious[db]"') from exc


# ------------------------------------------------------------------------------------------- instrumentation
def _bump_in_use(db: str, delta: int) -> None:
    with _in_use_lock:
        value = max(0, _in_use[db] + delta)
        _in_use[db] = value
    DB_POOL_IN_USE.labels(db=db).set(value)


def pool_capacity(pool: Any) -> int:
    """Max connections a pool hands out at once (0 = unbounded / unknown)."""
    size = getattr(pool, "size", None)
    try:
        if callable(size):  # QueuePool
            n = int(size())
            overflow = int(getattr(pool, "_max_overflow", 0) or 0)
            return 0 if n == 0 else n + max(0, overflow)
        if isinstance(size, int):  # SingletonThreadPool
            return size
    except Exception:  # noqa: BLE001
        return 0
    return 1 if type(pool).__name__ in {"StaticPool", "AssertionPool"} else 0


def _op(statement: str) -> str:
    head = (statement or "").lstrip()[:8].lower()
    for op in ("select", "insert", "update"):
        if head.startswith(op):
            return op
    return "other"


def _patch_pool_wait(pool: Any, db: str) -> None:
    """Time ``pool.connect()`` (what Engine.connect() calls) from the start of the checkout to getting a connection."""
    if getattr(pool, "_insidious_wait_patched", False):
        return
    original = pool.connect

    def connect() -> Any:
        t0 = time.perf_counter()
        try:
            return original()
        finally:
            DB_POOL_WAIT_SECONDS.labels(db=db).observe(time.perf_counter() - t0)

    pool.connect = connect
    pool._insidious_wait_patched = True


def instrument_engine(engine: Any, db: str = "primary") -> Any:
    """Attach pool + cursor events to ``engine`` so it emits the insidious_db_* metrics. Idempotent. Returns engine."""
    _require_sqlalchemy()
    from sqlalchemy import event

    if engine in _instrumented:
        return engine
    _instrumented[engine] = db

    def on_checkout(dbapi_conn: Any, record: Any, proxy: Any) -> None:
        _bump_in_use(db, +1)

    def on_checkin(dbapi_conn: Any, record: Any) -> None:
        _bump_in_use(db, -1)

    def before_cursor(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        conn.info.setdefault("_insidious_q", []).append(time.perf_counter())

    def after_cursor(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        starts = conn.info.get("_insidious_q")
        if starts:
            DB_QUERY_SECONDS.labels(db=db, op=_op(statement)).observe(time.perf_counter() - starts.pop())

    def on_error(ctx: Any) -> None:
        conn = getattr(ctx, "connection", None)
        starts = conn.info.get("_insidious_q") if conn is not None else None
        if starts:
            DB_QUERY_SECONDS.labels(db=db, op=_op(getattr(ctx, "statement", "") or "")).observe(
                time.perf_counter() - starts.pop())

    def on_disposed(eng: Any) -> None:
        # Engine.dispose() swaps in a fresh pool: pool events carry over, the wait timer must be re-attached.
        _patch_pool_wait(eng.pool, db)

    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", on_checkin)
    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "after_cursor_execute", after_cursor)
    event.listen(engine, "handle_error", on_error)
    event.listen(engine, "engine_disposed", on_disposed)
    _patch_pool_wait(engine.pool, db)
    DB_POOL_SIZE.labels(db=db).set(pool_capacity(engine.pool))
    DB_POOL_IN_USE.labels(db=db).set(_in_use[db])
    return engine


def pool_stats(engine: Any) -> dict[str, Any]:
    """Live pool numbers for an engine (None where the pool type does not track them)."""
    pool = engine.pool

    def call(name: str) -> int | None:
        fn = getattr(pool, name, None)
        try:
            return int(fn()) if callable(fn) else None
        except Exception:  # noqa: BLE001
            return None

    return {
        "url": mask_url(engine.url),
        "dialect": engine.dialect.name,
        "pool_class": type(pool).__name__,
        "capacity": pool_capacity(pool),
        "in_use": call("checkedout"),
        "idle": call("checkedin"),
    }


# ------------------------------------------------------------------------------------------- config helpers
@dataclass(frozen=True)
class PoolConfig:
    pool_size: int = 5
    max_overflow: int = 0
    timeout_s: float = 2.0

    @classmethod
    def parse(cls, raw: Any, fallback: "PoolConfig | None" = None) -> "PoolConfig":
        """Parse the ``db_pool`` remote config (JSON string or mapping); invalid input keeps ``fallback``."""
        base = fallback or cls()
        if raw is None or raw == "":
            return base
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
            if not isinstance(data, Mapping):
                raise ValueError("db_pool must be a JSON object")
            size = int(data.get("pool_size", base.pool_size))
            overflow = int(data.get("max_overflow", base.max_overflow))
            timeout = float(data.get("timeout_s", base.timeout_s))
        except (TypeError, ValueError) as exc:
            _jlog(logging.WARNING, "db_pool_config_invalid", error=str(exc)[:200])
            return base
        return cls(pool_size=min(max(size, 1), 200), max_overflow=min(max(overflow, 0), 200),
                   timeout_s=min(max(timeout, 0.05), 120.0))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_url(url: str) -> str:
    """postgres:// and postgresql:// use psycopg 3 (the ``db`` extra) instead of SQLAlchemy's psycopg2 default."""
    url = (url or "").strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            try:
                import psycopg  # noqa: F401
            except ImportError:
                return "postgresql://" + url[len(prefix):]
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def mask_url(url: Any) -> str:
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "<unparseable url>"


def default_sqlite_url() -> str:
    return _env("INSIDIOUS_SQLITE_URL") or (
        "sqlite:///" + os.path.join(tempfile.gettempdir(), "insidious.db"))


# ------------------------------------------------------------------------------------------- routed database
class FlagRoutedDatabase:
    """Primary engine sized by Flagsmith ``db_pool``; reads go to a replica when ``read_from_replica`` is ON.

    ``flags`` is an Insidious, FlagClient, or anything with ``is_enabled(flag)`` and ``value(flag, default)``.
    Thread-safe. Flag state is re-checked at most every ``check_interval_s`` seconds (on use).
    """

    def __init__(
        self,
        flags: Any = None,
        primary_url: str | None = None,
        *,
        fallback_url: str | None = None,
        read_flag: str = READ_FLAG,
        read_url_config: str = READ_URL_CONFIG,
        pool_config: str = POOL_CONFIG,
        check_interval_s: float = 5.0,
        default_pool: PoolConfig | Mapping[str, Any] | None = None,
        engine_kwargs: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _require_sqlalchemy()
        self.flags = flags
        raw = primary_url or os.environ.get("DATABASE_URL") or fallback_url or default_sqlite_url()
        self.primary_url = normalize_url(raw)
        self.read_flag = read_flag
        self.read_url_config = read_url_config
        self.pool_config_name = pool_config
        self.check_interval_s = float(check_interval_s)
        if isinstance(default_pool, PoolConfig):
            self.default_pool = default_pool
        else:
            self.default_pool = PoolConfig.parse(dict(default_pool) if default_pool else None)
        self.engine_kwargs = dict(engine_kwargs or {})
        self.clock = clock
        self.rebuilds = 0
        self._lock = threading.RLock()
        self._primary: Any = None
        self._primary_cfg: PoolConfig | None = None
        self._replica: Any = None
        self._replica_url: str | None = None
        self._replica_cfg: PoolConfig | None = None
        self._read_flag_on = False
        self._last_check: float | None = None
        self.refresh(force=True)

    # ------------------------------------------------------------------ flags
    def _flag_on(self, name: str) -> bool:
        if self.flags is None:
            return False
        try:
            return bool(self.flags.is_enabled(name))
        except Exception as exc:  # noqa: BLE001
            _jlog(logging.WARNING, "db_flag_error", flag=name, error=str(exc)[:200])
            return False

    def _flag_value(self, name: str, default: Any = None) -> Any:
        if self.flags is None:
            return default
        try:
            return self.flags.value(name, default)
        except Exception as exc:  # noqa: BLE001
            _jlog(logging.WARNING, "db_flag_error", flag=name, error=str(exc)[:200])
            return default

    def desired_pool(self) -> PoolConfig:
        """Pool config from the flag; keys it omits use the defaults, invalid JSON keeps the running config."""
        raw = self._flag_value(self.pool_config_name, None)
        cfg = PoolConfig.parse(raw, self.default_pool)
        if cfg is self.default_pool and raw not in (None, "") and self._primary_cfg is not None:
            return self._primary_cfg
        return cfg

    # ------------------------------------------------------------------ engines
    def _build(self, url: str, cfg: PoolConfig, label: str) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import StaticPool

        u = make_url(url)
        kwargs = dict(self.engine_kwargs)
        connect_args = dict(kwargs.pop("connect_args", {}) or {})
        backend = u.get_backend_name()
        if backend == "sqlite":
            connect_args.setdefault("check_same_thread", False)
            connect_args.setdefault("timeout", 30)
            if u.database in (None, "", ":memory:") or (u.query.get("mode") == "memory"):
                kwargs.setdefault("poolclass", StaticPool)  # one shared connection keeps the in-memory DB alive
            else:
                kwargs.update(pool_size=cfg.pool_size, max_overflow=cfg.max_overflow, pool_timeout=cfg.timeout_s)
        else:
            if backend == "postgresql":
                connect_args.setdefault("connect_timeout", 3)
            kwargs.update(pool_size=cfg.pool_size, max_overflow=cfg.max_overflow, pool_timeout=cfg.timeout_s)
            kwargs.setdefault("pool_pre_ping", True)
            kwargs.setdefault("pool_recycle", 1800)
        engine = create_engine(u, connect_args=connect_args, **kwargs)
        instrument_engine(engine, db=label)
        _jlog(logging.INFO, "db_engine_built", db=label, url=mask_url(u), **cfg.as_dict())
        return engine

    def refresh(self, force: bool = False) -> bool:
        """Re-read flags (rate-limited) and rebuild engines if needed. Returns True when an engine changed."""
        now = self.clock()
        if not force and self._last_check is not None and now - self._last_check < self.check_interval_s:
            return False
        old: list[Any] = []
        changed = False
        with self._lock:
            if not force and self._last_check is not None and now - self._last_check < self.check_interval_s:
                return False
            self._last_check = now
            cfg = self.desired_pool()
            on = self._flag_on(self.read_flag)
            raw_read = str(self._flag_value(self.read_url_config, "") or "").strip() if on else ""
            want_replica = normalize_url(raw_read) if raw_read else None

            if self._primary is None or cfg != self._primary_cfg:
                if self._primary is None:
                    new = self._build(self.primary_url, cfg, "primary")  # first build: errors surface to caller
                else:
                    try:
                        new = self._build(self.primary_url, cfg, "primary")
                    except Exception as exc:  # noqa: BLE001 - keep serving on the running pool
                        _jlog(logging.WARNING, "db_pool_resize_failed", error=type(exc).__name__,
                              url=mask_url(self.primary_url))
                        new = None
                        cfg = self._primary_cfg  # type: ignore[assignment]
                if new is not None and self._primary is not None:
                    old.append(self._primary)
                    self.rebuilds += 1
                    _jlog(logging.INFO, "db_pool_resized", db="primary",
                          old=self._primary_cfg.as_dict() if self._primary_cfg else None, new=cfg.as_dict())
                if new is not None:
                    self._primary, self._primary_cfg = new, cfg
                    changed = True

            if want_replica != self._replica_url or (want_replica and cfg != self._replica_cfg):
                if self._replica is not None:
                    old.append(self._replica)
                self._replica, self._replica_url, self._replica_cfg = None, None, None
                if want_replica:
                    try:
                        self._replica = self._build(want_replica, cfg, "replica")
                        self._replica_url, self._replica_cfg = want_replica, cfg
                    except Exception as exc:  # noqa: BLE001 - a bad replica URL must never break reads
                        # type only: parse errors echo the raw URL, which may contain a password
                        _jlog(logging.WARNING, "db_replica_unusable", error=type(exc).__name__,
                              url=mask_url(want_replica))
                if self._replica is None:
                    DB_POOL_SIZE.labels(db="replica").set(0)
                changed = True
            if on != self._read_flag_on:
                _jlog(logging.INFO, "db_read_routing", read_from_replica=on,
                      reads_from="replica" if self._replica is not None else "primary")
            self._read_flag_on = on
        for engine in old:  # outside the lock; in-flight connections finish on the old pool
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                pass
        return changed

    def primary(self) -> Any:
        self.refresh()
        return self._primary

    write = primary

    def read(self) -> Any:
        """Replica engine when routing is on and a replica URL is configured, else the primary."""
        self.refresh()
        replica = self._replica
        return replica if replica is not None else self._primary

    @property
    def reads_from(self) -> str:
        return "replica" if self._replica is not None else "primary"

    @property
    def pool_config(self) -> PoolConfig | None:
        return self._primary_cfg

    def stats(self) -> dict[str, Any]:
        """``{"primary": {...}, "replica": {...} | None, "read_from_replica": bool, ...}`` for GET /api/db."""
        self.refresh()
        with self._lock:
            primary, pcfg = self._primary, self._primary_cfg
            replica, rcfg = self._replica, self._replica_cfg
            on = self._read_flag_on

        def one(engine: Any, cfg: PoolConfig | None) -> dict[str, Any] | None:
            if engine is None:
                return None
            return {**pool_stats(engine), **(cfg.as_dict() if cfg else {})}

        return {
            "primary": one(primary, pcfg),
            "replica": one(replica, rcfg),
            "read_from_replica": on,
            "reads_from": "replica" if replica is not None else "primary",
            "rebuilds": self.rebuilds,
            "check_interval_s": self.check_interval_s,
        }

    def dispose(self) -> None:
        with self._lock:
            engines = [e for e in (self._primary, self._replica) if e is not None]
            self._replica, self._replica_url, self._replica_cfg = None, None, None
        for e in engines:
            try:
                e.dispose()
            except Exception:  # noqa: BLE001
                pass
        DB_POOL_SIZE.labels(db="replica").set(0)
