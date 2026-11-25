"""insidious.db: pool/query metrics, live pool resize from db_pool, replica routing on read_from_replica."""
import threading
from pathlib import Path

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from prometheus_client import REGISTRY  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from insidious import Insidious, FlagClient, FlagRoutedDatabase, PoolConfig, instrument_engine, load_flags  # noqa: E402
from insidious.db import normalize_url, pool_capacity  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def flags():
    return FlagClient(load_flags(REPO / "flagsmith" / "flags.yaml"))


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def db(flags, tmp_path, clock):
    d = FlagRoutedDatabase(flags, f"sqlite:///{tmp_path / 'primary.db'}", clock=clock)
    yield d
    d.dispose()


def test_instrument_engine_metrics_move(tmp_path):
    eng = instrument_engine(create_engine(f"sqlite:///{tmp_path / 'm.db'}", pool_size=3, max_overflow=1), db="metrics")
    assert instrument_engine(eng, db="metrics") is eng  # idempotent
    assert sample("insidious_db_pool_size", db="metrics") == 4
    q = {op: sample("insidious_db_query_seconds_count", db="metrics", op=op)
         for op in ("select", "insert", "update", "other")}
    waits = sample("insidious_db_pool_wait_seconds_count", db="metrics")
    with eng.begin() as conn:
        assert sample("insidious_db_pool_in_use", db="metrics") == 1
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"))
        conn.execute(text("INSERT INTO t (v) VALUES (1)"))
        conn.execute(text("UPDATE t SET v = 2"))
        assert conn.execute(text("SELECT v FROM t")).scalar() == 2
    assert sample("insidious_db_pool_in_use", db="metrics") == 0
    assert sample("insidious_db_pool_wait_seconds_count", db="metrics") == waits + 1
    for op in ("select", "insert", "update", "other"):
        assert sample("insidious_db_query_seconds_count", db="metrics", op=op) >= q[op] + 1, op
    # a failing statement is still timed and does not leak the timer stack
    with pytest.raises(sqlalchemy.exc.OperationalError):
        with eng.connect() as conn:
            conn.execute(text("SELECT * FROM missing_table"))
    assert sample("insidious_db_pool_in_use", db="metrics") == 0


def test_pool_wait_histogram_sees_saturation(tmp_path):
    eng = instrument_engine(create_engine(f"sqlite:///{tmp_path / 'w.db'}", pool_size=1, max_overflow=0,
                                          pool_timeout=0.15), db="waits")
    before = sample("insidious_db_pool_wait_seconds_sum", db="waits")
    held = eng.connect()
    assert sample("insidious_db_pool_in_use", db="waits") == 1
    with pytest.raises(sqlalchemy.exc.TimeoutError):
        eng.connect()
    held.close()
    assert sample("insidious_db_pool_wait_seconds_sum", db="waits") - before >= 0.15
    # Engine.dispose() swaps the pool; the wait timer must survive it
    eng.dispose()
    n = sample("insidious_db_pool_wait_seconds_count", db="waits")
    with eng.connect():
        pass
    assert sample("insidious_db_pool_wait_seconds_count", db="waits") == n + 1


def test_pool_config_parse():
    base = PoolConfig()
    assert base.as_dict() == {"pool_size": 5, "max_overflow": 0, "timeout_s": 2.0}
    assert PoolConfig.parse('{"pool_size":20,"max_overflow":5,"timeout_s":1}') == PoolConfig(20, 5, 1.0)
    assert PoolConfig.parse({"pool_size": 8}) == PoolConfig(8, 0, 2.0)
    assert PoolConfig.parse("not json", base) is base
    assert PoolConfig.parse("[1,2]", base) is base
    assert PoolConfig.parse("", base) is base
    assert PoolConfig.parse('{"pool_size": 0, "max_overflow": -3}') == PoolConfig(1, 0, 2.0)  # clamped


def test_defaults_from_flags_yaml(db):
    st = db.stats()
    assert st["primary"]["pool_size"] == 5 and st["primary"]["capacity"] == 5
    assert st["primary"]["dialect"] == "sqlite" and st["primary"]["in_use"] == 0
    assert st["replica"] is None and st["read_from_replica"] is False and st["reads_from"] == "primary"
    assert db.read() is db.primary() is db.write()
    assert sample("insidious_db_pool_size", db="primary") == 5


def test_pool_config_change_rebuilds_engine(db, flags, clock):
    first = db.primary()
    flags.set_dev_overrides({"db_pool": '{"pool_size":12,"max_overflow":3,"timeout_s":1}'})
    assert db.primary() is first  # rate-limited: at most one flag check per 5 s
    clock.t += 5
    second = db.primary()
    assert second is not first
    assert db.rebuilds == 1
    assert db.pool_config == PoolConfig(12, 3, 1.0)
    assert second.pool.size() == 12 and second.pool.timeout() == 1.0
    assert sample("insidious_db_pool_size", db="primary") == 15
    clock.t += 5
    assert db.primary() is second  # unchanged config: no rebuild
    # invalid JSON keeps the running pool
    flags.set_dev_overrides({"db_pool": "{broken"})
    clock.t += 5
    assert db.primary() is second and db.rebuilds == 1


def test_rebuild_keeps_in_flight_connection_working(db, flags, clock):
    eng = db.primary()
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS k (v INTEGER)"))
    conn = eng.connect()
    flags.set_dev_overrides({"db_pool": '{"pool_size":2}'})
    clock.t += 5
    assert db.primary() is not eng
    conn.execute(text("SELECT count(*) FROM k")).scalar()  # old engine's checked-out connection still usable
    conn.close()
    assert sample("insidious_db_pool_in_use", db="primary") == 0


def test_replica_routing_on_flag(db, flags, clock, tmp_path):
    replica_url = f"sqlite:///{tmp_path / 'replica.db'}"
    with db.primary().begin() as conn:
        conn.execute(text("CREATE TABLE src (name TEXT)"))
        conn.execute(text("INSERT INTO src VALUES ('primary')"))
    replica_eng = create_engine(replica_url)
    with replica_eng.begin() as conn:
        conn.execute(text("CREATE TABLE src (name TEXT)"))
        conn.execute(text("INSERT INTO src VALUES ('replica')"))
    replica_eng.dispose()

    def who():
        with db.read().connect() as conn:
            return conn.execute(text("SELECT name FROM src")).scalar()

    assert who() == "primary"
    # URL set but flag off: still primary
    flags.set_dev_overrides({"db_read_url": replica_url})
    clock.t += 5
    assert who() == "primary" and db.stats()["replica"] is None
    # flag on: reads go to the replica, writes stay on the primary
    flags.set_dev_overrides({"read_from_replica": True})
    clock.t += 5
    assert who() == "replica"
    assert db.read() is not db.write()
    st = db.stats()
    assert st["read_from_replica"] is True and st["reads_from"] == "replica"
    assert st["replica"]["dialect"] == "sqlite" and st["replica"]["pool_size"] == 5
    assert sample("insidious_db_pool_size", db="replica") == 5
    n = sample("insidious_db_query_seconds_count", db="replica", op="select")
    who()
    assert sample("insidious_db_query_seconds_count", db="replica", op="select") == n + 1
    # flag on, URL empty: primary
    flags.set_dev_overrides({"db_read_url": ""})
    clock.t += 5
    assert who() == "primary" and db.stats()["replica"] is None
    assert sample("insidious_db_pool_size", db="replica") == 0
    # flag back off with URL: primary
    flags.set_dev_overrides({"db_read_url": replica_url, "read_from_replica": False})
    clock.t += 5
    assert who() == "primary"


def test_bad_replica_url_falls_back_to_primary(db, flags, clock):
    flags.set_dev_overrides({"read_from_replica": True, "db_read_url": "nonsense://///"})
    clock.t += 5
    assert db.read() is db.primary()
    assert db.stats()["reads_from"] == "primary"


def test_thread_safety_under_concurrent_refresh(db, flags, clock):
    with db.primary().begin() as conn:
        conn.execute(text("CREATE TABLE c (v INTEGER)"))
    errors = []

    def worker():
        try:
            for _ in range(20):
                with db.read().connect() as conn:
                    conn.execute(text("SELECT count(*) FROM c")).scalar()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for size in (2, 7, 3):
        flags.set_dev_overrides({"db_pool": f'{{"pool_size":{size},"max_overflow":2,"timeout_s":5}}'})
        clock.t += 5
        db.primary()
    for t in threads:
        t.join()
    assert not errors
    assert db.pool_config.pool_size == 3


def test_in_memory_sqlite_fallback(flags, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    d = FlagRoutedDatabase(flags, "sqlite://")
    with d.write().begin() as conn:
        conn.execute(text("CREATE TABLE m (v INTEGER)"))
        conn.execute(text("INSERT INTO m VALUES (7)"))
    with d.read().connect() as conn:  # same shared connection => same in-memory database
        assert conn.execute(text("SELECT v FROM m")).scalar() == 7
    assert d.stats()["primary"]["pool_class"] == "StaticPool"
    d.dispose()


def test_database_url_env_and_sqlite_file_fallback(flags, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    d = FlagRoutedDatabase(flags)
    assert str(tmp_path / "env.db") in d.stats()["primary"]["url"]
    d.dispose()
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("INSIDIOUS_SQLITE_URL", f"sqlite:///{tmp_path / 'fallback.db'}")
    d = FlagRoutedDatabase(flags)
    assert "fallback.db" in d.stats()["primary"]["url"]
    d.dispose()


def test_url_helpers():
    pytest.importorskip("psycopg")
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_postgres_engine_built_lazily_and_password_masked(flags):
    pytest.importorskip("psycopg")
    d = FlagRoutedDatabase(flags, "postgres://shop:s3cret@db.internal:5432/jeffy")  # never connects
    st = d.stats()["primary"]
    assert st["dialect"] == "postgresql" and st["capacity"] == 5
    assert "s3cret" not in st["url"] and "db.internal" in st["url"]
    d.dispose()


def test_pool_capacity_variants(tmp_path):
    from sqlalchemy.pool import NullPool, StaticPool

    assert pool_capacity(create_engine("sqlite://", poolclass=StaticPool).pool) == 1
    assert pool_capacity(create_engine(f"sqlite:///{tmp_path / 'n.db'}", poolclass=NullPool).pool) == 0
    assert pool_capacity(create_engine(f"sqlite:///{tmp_path / 'q.db'}", pool_size=4, max_overflow=2).pool) == 6


def test_facade_database(monkeypatch, tmp_path):
    monkeypatch.setenv("INSIDIOUS_CONFIG", str(REPO / "insidious.yaml"))
    monkeypatch.setenv("INSIDIOUS_FLAGS", str(REPO / "flagsmith" / "flags.yaml"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facade.db'}")
    monkeypatch.delenv("FLAGSMITH_SERVER_KEY", raising=False)
    fk = Insidious.from_env()
    db = fk.database()
    assert db is fk.database()
    assert db.flags is fk.flags
    with db.read().connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    # new flags never leak into GET /api/flags (db_read_url may hold a password)
    env = fk.env_states()
    assert not {"db_pool", "db_read_url", "agent_config", "read_from_replica", "ai_agent"} & set(env)
    assert sample("insidious_flag_state", flag="ai_agent") == 1
    assert REGISTRY.get_sample_value("insidious_flag_state", {"flag": "read_from_replica"}) == 0
    internal = fk.flags.env_states(include_internal=True)
    assert internal["db_read_url"] is False and internal["ai_agent"] is True
    fk.close()
