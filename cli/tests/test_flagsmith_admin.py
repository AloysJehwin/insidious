import json

import httpx
import respx

from insidious_cli.flagsmith_admin import FlagsmithAdmin, feature_payload, segment_payload

BASE = "https://fs.test/api/v1"
ENV = "envkey123"


def admin(**kw) -> FlagsmithAdmin:
    return FlagsmithAdmin(base_url=BASE, api_key="orgkey", project_id=42, client=httpx.Client(), **kw)


def test_dry_run_plans_everything_and_sends_nothing(spec):
    with respx.mock(assert_all_called=False) as router:  # any real request would raise (no routes)
        a = FlagsmithAdmin(base_url=BASE, api_key="orgkey", project_id=42, dry_run=True)
        results = a.apply_spec(spec, ENV)
        assert router.calls.call_count == 0
    posts = [(c.path, c.body) for c in a.calls if c.method == "POST"]
    feature_posts = [b for p, b in posts if p == "/projects/42/features/"]
    assert [b["name"] for b in feature_posts] == [f["name"] for f in spec["features"]]
    mv = next(b for b in feature_posts if b["name"] == "checkout_flow")
    assert mv["type"] == "MULTIVARIATE" and mv["initial_value"] == "full"
    assert {o["string_value"] for o in mv["multivariate_options"]} == {"full", "lite"}
    pg = next(b for b in feature_posts if b["name"] == "payment_gateway")
    assert pg["type"] == "STANDARD" and pg["initial_value"] == "razorpay"
    n_segments = len(spec["segments"])
    assert sum(p == "/projects/42/segments/" for p, _ in posts) == n_segments
    overrides = [b for p, b in posts if p.endswith("/create-segment-override/")]
    assert len(overrides) == sum(len(s.get("overrides", [])) for s in spec["segments"])
    assert overrides[0]["enabled"] is True
    assert overrides[0]["feature_segment"] == {"segment": "<high_value_cart.id>"}
    assert sum("/dependencies/" in p for p, _ in posts) == 3
    assert all(r.action == "planned" for r in results)
    assert all(c.sent is False for c in a.calls)


def test_toggle_flag_patches_environment_level_state():
    with respx.mock as router:
        lst = router.get(f"{BASE}/environments/{ENV}/featurestates/").mock(return_value=httpx.Response(200, json={
            "count": 3, "next": None, "results": [
                {"id": 11, "feature": 1, "identity": 5, "feature_segment": None, "enabled": True},
                {"id": 12, "feature": 1, "identity": None, "feature_segment": 7, "enabled": True},
                {"id": 13, "feature": 1, "identity": None, "feature_segment": None, "enabled": True},
            ]}))
        patch = router.patch(f"{BASE}/environments/{ENV}/featurestates/13/").mock(
            return_value=httpx.Response(200, json={"id": 13, "enabled": False}))
        admin().toggle_flag(ENV, "heavy_features", False)
    assert lst.calls[0].request.url.params["feature_name"] == "heavy_features"
    req = patch.calls[0].request
    assert json.loads(req.content) == {"enabled": False}
    assert req.headers["Authorization"] == "Api-Key orgkey"


def test_set_value_sends_feature_state_value():
    with respx.mock as router:
        router.get(f"{BASE}/environments/{ENV}/featurestates/").mock(return_value=httpx.Response(200, json=[
            {"id": 21, "identity": None, "feature_segment": None}]))
        patch = router.patch(f"{BASE}/environments/{ENV}/featurestates/21/").mock(
            return_value=httpx.Response(200, json={}))
        admin().set_value(ENV, "payment_gateway", "cashfree")
    assert json.loads(patch.calls[0].request.content) == {"feature_state_value": "cashfree"}


def test_apply_is_idempotent_when_everything_exists(spec):
    feats = [{"id": i + 1, "name": f["name"], "description": f.get("description")} for i, f in enumerate(spec["features"])]
    fid = {f["name"]: f["id"] for f in feats}
    with respx.mock as router:
        router.get(f"{BASE}/environments/{ENV}/").mock(return_value=httpx.Response(200, json={"id": 99}))
        # paginated list: first page links to a second page
        router.get(f"{BASE}/projects/42/features/", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"results": feats[4:], "next": None}))
        router.get(f"{BASE}/projects/42/features/").mock(return_value=httpx.Response(200, json={
            "results": feats[:4], "next": f"{BASE}/projects/42/features/?page=2"}))
        router.get(f"{BASE}/projects/42/features/{fid['checkout_flow']}/mv-options/").mock(
            return_value=httpx.Response(200, json=[{"string_value": "full"}, {"string_value": "lite"}]))
        router.get(url__regex=rf"{BASE}/environments/{ENV}/features/\d+/dependencies/").mock(
            return_value=httpx.Response(200, json={"results": [
                {"feature": {"id": 0}, "prerequisite": {"id": fid["heavy_features"], "name": "heavy_features"}}]}))
        router.get(f"{BASE}/projects/42/segments/").mock(return_value=httpx.Response(200, json={"results": [
            {"id": 501, "name": "high_value_cart"}, {"id": 502, "name": "festive_collection_rollout"}], "next": None}))
        router.get(f"{BASE}/features/feature-segments/").mock(side_effect=lambda req: httpx.Response(200, json={
            "results": [{"id": 9, "segment": 501 if req.url.params["feature"] == str(fid["heavy_features"]) else 502}]}))
        posts = router.post(url__startswith=BASE).mock(return_value=httpx.Response(201, json={"id": 1}))
        results = admin().apply_spec(spec, ENV)
    assert posts.call_count == 0
    assert {r.action for r in results} == {"exists"}


def test_dependency_refused_is_a_warning_not_a_failure(spec):
    small = {"features": [spec["features"][0], spec["features"][1]], "segments": []}
    with respx.mock as router:
        router.get(f"{BASE}/environments/{ENV}/").mock(return_value=httpx.Response(200, json={"id": 99}))
        router.get(f"{BASE}/projects/42/features/").mock(return_value=httpx.Response(200, json={"results": [], "next": None}))
        router.post(f"{BASE}/projects/42/features/").mock(side_effect=lambda req: httpx.Response(
            201, json={"id": 1 if json.loads(req.content)["name"] == "heavy_features" else 2}))
        router.get(f"{BASE}/environments/{ENV}/features/2/dependencies/").mock(return_value=httpx.Response(200, json={"results": []}))
        router.post(f"{BASE}/environments/{ENV}/features/2/dependencies/1/").mock(
            return_value=httpx.Response(403, json={"detail": "plan"}))
        router.get(f"{BASE}/projects/42/segments/").mock(return_value=httpx.Response(200, json={"results": []}))
        results = admin().apply_spec(small, ENV)
    dep = next(r for r in results if r.kind == "dependency")
    assert dep.action == "warning" and "403" in dep.detail
    assert [r.action for r in results if r.kind == "feature"] == ["created", "created"]


def test_payload_builders():
    seg = segment_payload({"name": "high_value_cart", "rules": [
        {"property": "cart_value", "operator": "GREATER_THAN", "value": "2000"}]})
    assert seg["rules"][0]["type"] == "ALL"
    cond = seg["rules"][0]["rules"][0]["conditions"][0]
    assert cond == {"operator": "GREATER_THAN", "property": "cart_value", "value": "2000"}
    f = feature_payload({"name": "heavy_features", "type": "FLAG", "default_enabled": True})
    assert f == {"name": "heavy_features", "type": "STANDARD", "default_enabled": True, "description": ""}
