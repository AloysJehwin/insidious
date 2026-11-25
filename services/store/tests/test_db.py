"""Store on the database: catalogue reads via the read engine, orders on the primary, /api/db, 503 until ready."""
import shutil
import threading
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select, text, update

from app import main
from app.db import StoreDB, orders, products
from app.main import app, fk, store
from insidious import FlagRoutedDatabase

PAY = "http://payments-mock.test:8001/pay"


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def reset_db_routing():
    fk.flags.clear_dev_overrides()
    fk.dev_mode = True
    main.sdb.db.refresh(force=True)
    yield
    fk.flags.clear_dev_overrides()
    main.sdb.db.refresh(force=True)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def pay_ok():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(PAY).mock(return_value=httpx.Response(
            200, json={"ok": True, "payment_id": "pay_db123", "gateway": "razorpay"}))
        yield route


def primary_rows(query):
    with main.sdb.db.primary().connect() as conn:
        return conn.execute(query).mappings().all()


def test_schema_seeded_idempotently(client):
    assert client.get("/api/products").status_code == 200
    assert main.sdb.ready.is_set()
    assert main.sdb.init_schema() == 0  # second run inserts nothing
    with main.sdb.db.primary().connect() as conn:
        assert conn.execute(select(func.count()).select_from(products)).scalar() == 12


def test_api_db_shape(client):
    client.get("/api/products")
    body = client.get("/api/db").json()
    assert set(body) >= {"primary", "replica", "read_from_replica"}
    assert body["replica"] is None and body["read_from_replica"] is False and body["reads_from"] == "primary"
    assert body["ready"] is True and body["last_error"] is None
    p = body["primary"]
    assert p["dialect"] == "sqlite" and p["pool_size"] == 5 and p["max_overflow"] == 0 and p["timeout_s"] == 2.0
    assert p["capacity"] == 5 and p["in_use"] == 0


def test_catalogue_comes_from_database(client):
    client.get("/api/products")
    with main.sdb.db.primary().begin() as conn:
        conn.execute(update(products).where(products.c.id == 4).values(price=777, name="DB Edited Jutti"))
    try:
        listed = {p["id"]: p for p in client.get("/api/products").json()["products"]}
        assert listed[4]["price"] == 777 and listed[4]["name"] == "DB Edited Jutti"
        assert client.get("/api/products/4").json()["name"] == "DB Edited Jutti"
        assert "DB Edited Jutti" in client.get("/product/4").text
    finally:
        with main.sdb.db.primary().begin() as conn:
            conn.execute(update(products).where(products.c.id == 4).values(price=999, name="Gulabi Nagri Pink Jutti"))


def test_reads_and_writes_emit_db_metrics(client, pay_ok):
    sel = sample("insidious_db_query_seconds_count", db="primary", op="select")
    ins = sample("insidious_db_query_seconds_count", db="primary", op="insert")
    upd = sample("insidious_db_query_seconds_count", db="primary", op="update")
    waits = sample("insidious_db_pool_wait_seconds_count", db="primary")
    client.get("/api/products")
    assert client.post("/api/checkout", json={}).status_code == 200
    assert sample("insidious_db_query_seconds_count", db="primary", op="select") > sel
    assert sample("insidious_db_query_seconds_count", db="primary", op="insert") == ins + 1
    assert sample("insidious_db_query_seconds_count", db="primary", op="update") == upd + 1
    assert sample("insidious_db_pool_wait_seconds_count", db="primary") > waits
    assert sample("insidious_db_pool_size", db="primary") == 5
    metrics = client.get("/metrics").text
    for name in ("insidious_db_pool_size", "insidious_db_pool_in_use", "insidious_db_pool_wait_seconds_bucket",
                 "insidious_db_query_seconds_bucket"):
        assert name in metrics


def test_order_persisted_on_primary(client, pay_ok):
    client.post("/api/cart", json={"product_id": 5, "qty": 1})
    client.post("/api/dev/flags", json={"checkout_flow": "lite", "payment_gateway": "cashfree"})
    r = client.post("/api/checkout", json={})
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    row = primary_rows(select(orders).where(orders.c.id == oid))[0]
    assert row["status"] == "paid" and row["payment_id"] == "pay_db123"
    assert row["total"] == 3499 and row["gateway"] == "cashfree" and row["flow"] == "lite"
    assert row["session_id"] == client.cookies.get("fk_sid")
    assert row["created_at"] is not None


def test_failed_payment_marks_order_failed(client):
    with respx.mock() as mock:
        mock.post(PAY).mock(return_value=httpx.Response(502, json={"ok": False, "error": "gateway_error"}))
        r = client.post("/api/checkout", json={})
    assert r.status_code == 502
    row = primary_rows(select(orders).where(orders.c.id == r.json()["order_id"]))[0]
    assert row["status"] == "failed" and row["payment_id"] is None


def test_order_page_rebuilt_from_db(client, pay_ok):
    client.post("/api/cart", json={"product_id": 7, "qty": 2})
    oid = client.post("/api/checkout", json={}).json()["order_id"]
    with store.lock:
        store.orders.pop(oid)  # as if the order was placed on another pod
    page = client.get(f"/order/{oid}")
    assert page.status_code == 200
    assert "Kundan Bridal Jutti" in page.text and oid in page.text
    assert client.get("/order/JFY-NOPE").status_code == 404


def test_pool_resized_live_from_flag(client):
    client.get("/api/products")
    before = client.get("/api/db").json()["rebuilds"]
    client.post("/api/dev/flags", json={"db_pool": '{"pool_size": 9, "max_overflow": 2, "timeout_s": 1.5}'})
    main.sdb.db.refresh(force=True)  # normally picked up within 5 s
    body = client.get("/api/db").json()
    assert body["primary"]["pool_size"] == 9 and body["primary"]["capacity"] == 11
    assert body["primary"]["timeout_s"] == 1.5 and body["rebuilds"] == before + 1
    assert sample("insidious_db_pool_size", db="primary") == 11
    assert client.get("/api/products").status_code == 200


def test_reads_follow_replica_flag_writes_stay_on_primary(client, pay_ok, tmp_path):
    client.get("/api/products")
    primary_path = main.sdb.db.primary().url.database
    replica_path = tmp_path / "replica.db"
    shutil.copy(primary_path, replica_path)
    replica_url = f"sqlite:///{replica_path}"
    from sqlalchemy import create_engine

    eng = create_engine(replica_url)
    with eng.begin() as conn:
        conn.execute(text("UPDATE products SET name = 'Replica Jutti' WHERE id = 3"))
        conn.execute(text("DELETE FROM orders"))
    eng.dispose()

    r = client.post("/api/dev/flags", json={"read_from_replica": True, "db_read_url": replica_url})
    assert r.status_code == 200
    main.sdb.db.refresh(force=True)
    secret = client.post("/api/dev/flags", json={"db_read_url": "postgresql://ro:hunter2@replica.internal/jeffy"})
    assert "hunter2" not in secret.text and "hunter2" not in client.get("/api/dev/flags").text
    client.post("/api/dev/flags", json={"db_read_url": replica_url})
    body = client.get("/api/db").json()
    assert body["read_from_replica"] is True and body["reads_from"] == "replica"
    assert body["replica"]["dialect"] == "sqlite" and body["replica"]["pool_size"] == 5
    assert str(replica_path) in body["replica"]["url"]

    n = sample("insidious_db_query_seconds_count", db="replica", op="select")
    assert client.get("/api/products/3").json()["name"] == "Replica Jutti"
    assert any(p["name"] == "Replica Jutti" for p in client.get("/api/products").json()["products"])
    assert client.get("/api/recommendations?pid=1").json()["source"] == "live"
    assert sample("insidious_db_query_seconds_count", db="replica", op="select") >= n + 3
    # the db_read_url secret never shows up in /api/flags
    assert "db_read_url" not in client.get("/api/flags").json()

    oid = client.post("/api/checkout", json={}).json()["order_id"]
    assert primary_rows(select(orders.c.status).where(orders.c.id == oid))[0]["status"] == "paid"
    eng = create_engine(replica_url)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == 0  # writes never hit the replica
    eng.dispose()

    client.delete("/api/dev/flags")
    main.sdb.db.refresh(force=True)
    assert client.get("/api/products/3").json()["name"] == "Sheesh Mahal Mirror Jutti"
    assert client.get("/api/db").json()["replica"] is None


def test_pool_exhaustion_returns_503(client):
    client.get("/api/products")
    client.post("/api/dev/flags", json={"db_pool": '{"pool_size": 1, "max_overflow": 0, "timeout_s": 0.1}'})
    main.sdb.db.refresh(force=True)
    held = main.sdb.db.primary().connect()
    try:
        waited = sample("insidious_db_pool_wait_seconds_sum", db="primary")
        r = client.get("/api/products")
        assert r.status_code == 503 and r.json()["error"] == "db_unavailable"
        assert sample("insidious_db_pool_wait_seconds_sum", db="primary") - waited >= 0.1
        assert client.get("/api/db").json()["primary"]["in_use"] == 1
        # recommendations degrade to cached instead of failing
        assert client.get("/api/recommendations?pid=2").json()["source"] == "cached"
    finally:
        held.close()
    assert client.get("/api/products").status_code == 200


def test_query_work_ms_adds_cost(client):
    client.get("/api/products")
    old = main.sdb.work_ms
    main.sdb.work_ms = 40
    try:
        t0 = time.perf_counter()
        assert client.get("/api/products").status_code == 200
        assert time.perf_counter() - t0 >= 0.04
    finally:
        main.sdb.work_ms = old


def test_db_unreachable_503_until_ready(client, monkeypatch):
    pytest.importorskip("psycopg")
    # nothing listens on port 1: connection refused immediately (local only, no network)
    down = StoreDB(FlagRoutedDatabase(fk.flags, "postgresql://fk:pw@127.0.0.1:1/jeffy", check_interval_s=60),
                   work_ms=0)
    monkeypatch.setattr(main, "sdb", down)
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/api/products")
    assert r.status_code == 503 and r.json()["error"] == "db_unavailable"
    assert client.get("/api/products/3").status_code == 503
    assert client.get("/api/recommendations?pid=1").status_code == 503
    assert client.post("/api/checkout", json={}).status_code == 503
    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 200
    info = client.get("/api/db").json()
    assert info["ready"] is False and info["last_error"] and "pw" not in str(info)
    assert client.post("/api/cart", json={"product_id": 2, "qty": 1}).status_code == 200  # carts are in memory
    down.close()


def test_background_init_retries_with_backoff(tmp_path):
    db = FlagRoutedDatabase(fk.flags, f"sqlite:///{tmp_path / 'retry.db'}")
    sdb = StoreDB(db, work_ms=0)
    real = sdb.init_schema
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is starting up")
        return real()

    sdb.init_schema = flaky
    sdb.start(initial_backoff_s=0.02, max_backoff_s=0.05)
    assert sdb.ready.wait(5)
    assert calls["n"] == 3 and sdb.attempts == 3 and sdb.last_error is None
    assert len(sdb.list_products()) == 12
    sdb.close()


def test_concurrent_checkouts(pay_ok):
    errors = []

    def shopper():
        c = TestClient(app)
        try:
            for _ in range(3):
                r = c.post("/api/checkout", json={})
                assert r.status_code == 200, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=shopper) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
