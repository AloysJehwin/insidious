import pytest
from fastapi.testclient import TestClient

from app.main import app, initial_chaos


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("PAY_RAZORPAY_LATENCY_MS", "1")
    monkeypatch.setenv("PAY_CASHFREE_LATENCY_MS", "1")
    app.state.chaos = initial_chaos()
    app.state.jitter_ms = 0
    return TestClient(app)


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_pay_ok(client):
    r = client.post("/pay", json={"gateway": "razorpay", "amount": 2499, "order_id": "o1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["gateway"] == "razorpay" and body["order_id"] == "o1"
    assert body["payment_id"].startswith("pay_")


def test_unknown_gateway(client):
    assert client.post("/pay", json={"gateway": "paypal", "amount": 1}).status_code == 400


def test_chaos_error_rate_and_latency(client):
    r = client.post("/chaos", json={"gateway": "razorpay", "error_rate": 1.0, "latency_ms": 5})
    assert r.json() == {"gateway": "razorpay", "latency_ms": 5.0, "error_rate": 1.0}
    r = client.post("/pay", json={"gateway": "razorpay", "amount": 10})
    assert r.status_code == 502 and r.json()["error"] == "gateway_error"
    # cashfree unaffected -> failover target works
    assert client.post("/pay", json={"gateway": "cashfree", "amount": 10}).status_code == 200
    state = client.get("/chaos").json()["gateways"]
    assert state["razorpay"]["error_rate"] == 1.0
    client.post("/chaos/reset")
    assert client.post("/pay", json={"gateway": "razorpay", "amount": 10}).status_code == 200


def test_chaos_clamps(client):
    r = client.post("/chaos", json={"gateway": "cashfree", "error_rate": 7, "latency_ms": -3})
    assert r.json()["error_rate"] == 1.0 and r.json()["latency_ms"] == 0.0


def test_env_knobs(monkeypatch):
    monkeypatch.setenv("PAY_RAZORPAY_ERROR_RATE", "0.25")
    monkeypatch.setenv("PAY_CASHFREE_LATENCY_MS", "333")
    c = initial_chaos()
    assert c["razorpay"]["error_rate"] == 0.25 and c["cashfree"]["latency_ms"] == 333
