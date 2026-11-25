import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import catalog
from app.main import app, fk, inr, store

PAY = "http://payments-mock.test:8001/pay"
GUARD = "http://guardian.test:8002/api/state"


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def reset():
    fk.flags.clear_dev_overrides()
    fk.dev_mode = True
    yield
    fk.flags.clear_dev_overrides()
    fk.dev_mode = True


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def pay_ok():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(PAY).mock(return_value=httpx.Response(
            200, json={"ok": True, "payment_id": "pay_test123", "gateway": "razorpay"}))
        yield route


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_inr_grouping():
    assert inr(999) == "₹999"
    assert inr(2499) == "₹2,499"
    assert inr(149999) == "₹1,49,999"
    assert inr(12345678) == "₹1,23,45,678"


def test_api_products_and_cookie(client):
    r = client.get("/api/products")
    assert r.status_code == 200
    body = r.json()
    assert len(body["products"]) == 12
    assert all(p["currency"] == "INR" and p["price"] > 0 for p in body["products"])
    assert "fk_sid" in r.cookies


def test_api_product_detail_hd_and_shed(client):
    r = client.get("/api/products/3").json()
    assert r["image_quality"] == "hd" and r["reviews"]["source"] == "live"
    client.post("/api/dev/flags", json={"heavy_features": False})
    r = client.get("/api/products/3").json()
    assert r["image_quality"] == "standard" and r["reviews"]["source"] == "cached"
    assert client.get("/api/products/999").status_code == 404


def test_api_recommendations_live_then_cached(client):
    r = client.get("/api/recommendations?pid=1").json()
    assert r["source"] == "live" and len(r["items"]) == 4
    assert all(i["id"] != 1 for i in r["items"])
    before = sample("insidious_feature_shed_total", feature="recommendations")
    client.post("/api/dev/flags", json={"recommendations": False})
    r = client.get("/api/recommendations?pid=1").json()
    assert r["source"] == "cached"
    assert sample("insidious_feature_shed_total", feature="recommendations") == before + 1


def test_api_cart_and_checkout(client, pay_ok):
    r = client.post("/api/cart", json={"product_id": 3, "qty": 2})
    assert r.status_code == 200 and r.json()["cart_value"] == 2 * catalog.get(3).price
    assert client.get("/api/cart").json()["cart_value"] == 2 * catalog.get(3).price
    client.post("/api/dev/flags", json={"checkout_flow": "full"})
    before = sample("insidious_orders_total", checkout_flow="full", gateway="razorpay")
    r = client.post("/api/checkout", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["total"] == 2 * catalog.get(3).price
    assert body["checkout_flow"] == "full" and body["gateway"] == "razorpay"
    assert body["recommendations_source"] == "live" and len(body["frequently_bought_together"]) == 3
    assert pay_ok.called
    sent = pay_ok.calls.last.request
    assert b'"gateway":"razorpay"' in sent.content.replace(b" ", b"")
    assert sample("insidious_orders_total", checkout_flow="full", gateway="razorpay") == before + 1
    assert client.get("/api/cart").json()["cart_value"] == 0  # cart cleared
    # checkout route is recorded under the SLO route label guardian queries
    assert sample("http_requests_total", route="/checkout", method="POST", status="200") >= 1


def test_api_checkout_empty_cart_autofills(client, pay_ok):
    r = client.post("/api/checkout", json={})
    assert r.status_code == 200 and r.json()["total"] > 0


def test_checkout_lite_skips_recommendations(client, pay_ok):
    client.post("/api/dev/flags", json={"checkout_flow": "lite"})
    r = client.post("/api/checkout", json={}).json()
    assert r["checkout_flow"] == "lite" and r["recommendations_source"] == "skipped"


def test_checkout_full_with_recs_shed(client, pay_ok):
    client.post("/api/dev/flags", json={"checkout_flow": "full", "heavy_features": False})
    r = client.post("/api/checkout", json={}).json()
    assert r["recommendations_source"] == "cached"


def test_checkout_uses_payment_gateway_remote_config(client, pay_ok):
    client.post("/api/dev/flags", json={"payment_gateway": "cashfree"})
    r = client.post("/api/checkout", json={}).json()
    assert r["gateway"] == "cashfree"
    assert b"cashfree" in pay_ok.calls.last.request.content


def test_checkout_gateway_failure_returns_502(client):
    before = sample("insidious_payment_failures_total", gateway="razorpay")
    with respx.mock() as mock:
        mock.post(PAY).mock(return_value=httpx.Response(502, json={"ok": False, "error": "gateway_error"}))
        r = client.post("/api/checkout", json={})
    assert r.status_code == 502 and r.json()["error"] == "payment_failed"
    assert sample("insidious_payment_failures_total", gateway="razorpay") == before + 1
    assert sample("http_requests_total", route="/checkout", method="POST", status="502") >= 1


def test_checkout_gateway_unreachable_returns_502(client):
    with respx.mock() as mock:
        mock.post(PAY).mock(side_effect=httpx.ConnectError("refused"))
        r = client.post("/api/checkout", json={})
    assert r.status_code == 502


def test_api_flags_contract(client):
    r = client.get("/api/flags").json()
    assert r == {
        "heavy_features": True, "recommendations": True, "hd_images": True, "live_reviews": True,
        "checkout_flow": "full", "payment_gateway": "razorpay", "festive_collection": False, "source": "offline",
    }


def test_dev_flags_set_and_parent(client):
    r = client.post("/api/dev/flags", json={"heavy_features": False})
    assert r.status_code == 200
    flags = client.get("/api/flags").json()
    assert flags["heavy_features"] is False and flags["recommendations"] is False and flags["live_reviews"] is False
    assert client.get("/api/dev/flags").json()["overrides"] == {"heavy_features": False}
    assert client.post("/api/dev/flags", json={"nope": True}).status_code == 400
    client.delete("/api/dev/flags")
    assert client.get("/api/flags").json()["recommendations"] is True


def test_dev_flags_404_when_dev_off(client):
    fk.dev_mode = False
    assert client.post("/api/dev/flags", json={"heavy_features": False}).status_code == 404
    assert client.get("/api/dev/flags").status_code == 404
    assert client.delete("/api/dev/flags").status_code == 404
    assert client.get("/api/flags").json()["heavy_features"] is True


def test_metrics_endpoint(client):
    client.get("/product/2")
    body = client.get("/metrics").text
    assert 'route="/product/{id}"' in body
    assert "insidious_flag_state" in body
    assert 'route="/product/2"' not in body


def test_pages_render(client, pay_ok):
    assert "Walk into every" in client.get("/").text
    page = client.get("/product/7").text
    assert "Kundan Bridal Jutti" in page and "₹4,299" in page
    assert client.get("/product/99").status_code == 404
    r = client.post("/cart/add", data={"product_id": 7, "qty": 1}, follow_redirects=False)
    assert r.status_code == 303
    assert "Kundan Bridal Jutti" in client.get("/cart").text
    co = client.get("/checkout").text
    assert "checkout_flow" in co
    r = client.post("/checkout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/order/")
    assert "Your juttis are on their way" in client.get(r.headers["location"]).text


def test_checkout_page_variants(client):
    client.post("/cart/add", data={"product_id": 2, "qty": 1})
    client.post("/api/dev/flags", json={"checkout_flow": "lite"})
    assert "Express checkout" in client.get("/checkout").text
    client.post("/api/dev/flags", json={"checkout_flow": "full"})
    page = client.get("/checkout").text
    assert "Frequently bought together" in page and "1 · Address" in page


def test_home_shows_bestsellers_when_shed(client):
    assert "Picked for you" in client.get("/").text
    client.post("/api/dev/flags", json={"heavy_features": False})
    page = client.get("/").text
    assert "Bestsellers this Diwali" in page and "Standard images" in page


def test_high_value_cart_trait_sent(client, monkeypatch):
    seen = []
    orig = fk.flags.evaluate

    def spy(identity=None, traits=None):
        seen.append((identity, dict(traits or {})))
        return orig(identity, traits)

    monkeypatch.setattr(fk.flags, "evaluate", spy)
    client.post("/api/cart", json={"product_id": 7, "qty": 1})
    client.get("/api/recommendations")
    assert any(t.get("cart_value") == catalog.get(7).price for _, t in seen)
    assert all(i for i, _ in seen)


def test_svg_image(client):
    r = client.get("/img/5.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg")


def test_ops_page_and_demo(client):
    page = client.get("/ops").text
    assert "MISSION CONTROL" in page and "chart.umd.min.js" in page and "4.4.1" in page
    assert client.get("/ops?demo=1").status_code == 200


def test_ops_state_offline(client):
    with respx.mock() as mock:
        mock.get(GUARD).mock(side_effect=httpx.ConnectError("no route"))
        r = client.get("/ops/state").json()
    assert r["guardian_online"] is False and "ConnectError" in r["error"]
    assert r["store"]["flags"]["heavy_features"] is True


def test_ops_state_proxies_guardian(client):
    state = {"armed": True, "slo": {"/checkout": {"p95_ms": 1840.0, "target_p95_ms": 800, "breaching": True}}}
    with respx.mock() as mock:
        mock.get(GUARD).mock(return_value=httpx.Response(200, json=state))
        r = client.get("/ops/state").json()
    assert r["guardian_online"] is True and r["state"] == state


def test_experiment_summary(client, pay_ok):
    client.post("/api/dev/flags", json={"checkout_flow": "lite"})
    client.post("/api/checkout", json={})
    exp = client.get("/api/experiment").json()["variants"]
    assert exp["lite"]["conversions"] >= 1


def test_store_bounded():
    s = type(store)(max_carts=3)
    for i in range(10):
        s.add(f"sid{i}", 1)
    assert len(s.carts) == 3
