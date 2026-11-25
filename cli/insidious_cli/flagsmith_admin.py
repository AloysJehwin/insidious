"""Every Flagsmith Admin API call Insidious makes lives here.

Base `$FLAGSMITH_ADMIN_URL` (e.g. https://api.flagsmith.com/api/v1), header `Authorization: Api-Key <org key>`.
Paths checked against https://api.flagsmith.com/api/v1/swagger.json (OpenAPI 3.1):

  GET   /environments/{env_key}/                                        environment (id, v2 versioning flag)
  GET   /projects/{project}/features/                                    list features (paginated)
  POST  /projects/{project}/features/                                    create feature (STANDARD|MULTIVARIATE)
  PATCH /projects/{project}/features/{id}/                               update description
  GET   /projects/{project}/features/{id}/mv-options/                    list multivariate options
  POST  /projects/{project}/features/{id}/mv-options/                    add multivariate option
  GET   /environments/{env_key}/features/{id}/dependencies/              list flag dependencies
  POST  /environments/{env_key}/features/{id}/dependencies/{prereq_id}/  add flag dependency
  GET   /projects/{project}/segments/                                    list segments (paginated)
  POST  /projects/{project}/segments/                                    create segment
  GET   /features/feature-segments/?environment=&feature=                list segment overrides of a feature
  POST  /environments/{env_key}/features/{id}/create-segment-override/   create segment override
  GET   /environments/{env_key}/featurestates/?feature_name=             feature states (legacy, no v2 versioning)
  PATCH /environments/{env_key}/featurestates/{id}/                      {"enabled": ...} / {"feature_state_value": ...}

Note: the Swagger serializer for the featurestate PATCH lists only `feature` and `enabled`; `feature_state_value`
is the legacy field CONTRACTS.md relies on for remote config values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

UNSET: Any = object()


class FlagsmithError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, Any] | None = None
    body: Any = None
    note: str = ""
    sent: bool = False
    status: int | None = None


@dataclass
class ApplyResult:
    kind: str  # feature | mv-option | dependency | segment | override | environment
    name: str
    action: str  # created | exists | updated | planned | skipped | failed | warning
    detail: str = ""


@dataclass
class FlagsmithAdmin:
    base_url: str
    api_key: str
    project_id: str | int
    dry_run: bool = False
    client: httpx.Client | None = None
    timeout: float = 15.0
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.client is None and not self.dry_run:
            self.client = httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------ transport
    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Api-Key {self.api_key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None, note: str = "",
              dry_result: Any = None) -> Any:
        call = Call(method, path, params, body, note)
        self.calls.append(call)
        if self.dry_run:
            return dry_result
        assert self.client is not None
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            r = self.client.request(method, url, params=params, json=body, headers=self.headers)
        except httpx.HTTPError as e:
            raise FlagsmithError(f"{method} {path}: {type(e).__name__}: {e}") from e
        call.sent, call.status = True, r.status_code
        if r.status_code >= 400:
            raise FlagsmithError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def _list(self, path: str, params: dict | None = None, note: str = "") -> list[dict]:
        """GET a (possibly paginated) list endpoint and return all rows."""
        out: list[dict] = []
        data = self._call("GET", path, params=params, note=note, dry_result=[])
        for _ in range(50):  # hard stop on pagination loops
            if isinstance(data, list):
                out.extend(data)
                break
            if not isinstance(data, dict):
                break
            out.extend(data.get("results") or [])
            nxt = data.get("next")
            if not nxt:
                break
            data = self._call("GET", nxt, note=f"{note} (next page)", dry_result=[])
        return out

    # ------------------------------------------------------------ environment
    def get_environment(self, env_key: str) -> dict:
        return self._call("GET", f"/environments/{env_key}/", note="look up environment id",
                          dry_result={"id": "<environment.id>", "api_key": env_key}) or {}

    # ------------------------------------------------------------ features
    def list_features(self) -> list[dict]:
        return self._list(f"/projects/{self.project_id}/features/", {"page_size": 100}, "list features")

    def create_feature(self, spec: dict) -> dict:
        body = feature_payload(spec)
        return self._call("POST", f"/projects/{self.project_id}/features/", body=body,
                          note=f"create feature {spec['name']}",
                          dry_result={"id": f"<{spec['name']}.id>", "name": spec["name"]}) or {}

    def update_feature_description(self, feature_id: Any, name: str, description: str) -> dict:
        return self._call("PATCH", f"/projects/{self.project_id}/features/{feature_id}/",
                          body={"name": name, "description": description},
                          note=f"update description of {name}") or {}

    def list_mv_options(self, feature_id: Any) -> list[dict]:
        return self._list(f"/projects/{self.project_id}/features/{feature_id}/mv-options/", None,
                          "list multivariate options")

    def create_mv_option(self, feature_id: Any, value: str, percentage: float) -> dict:
        body = {"type": "unicode", "string_value": str(value), "default_percentage_allocation": float(percentage),
                "feature": feature_id}
        return self._call("POST", f"/projects/{self.project_id}/features/{feature_id}/mv-options/", body=body,
                          note=f"add variant {value} ({percentage}%)") or {}

    def list_dependencies(self, env_key: str, feature_id: Any) -> list[dict]:
        return self._list(f"/environments/{env_key}/features/{feature_id}/dependencies/", None,
                          "list flag dependencies")

    def add_dependency(self, env_key: str, feature_id: Any, prerequisite_id: Any, note: str = "") -> dict:
        return self._call("POST", f"/environments/{env_key}/features/{feature_id}/dependencies/{prerequisite_id}/",
                          note=note or "add flag dependency") or {}

    # ------------------------------------------------------------ segments
    def list_segments(self) -> list[dict]:
        return self._list(f"/projects/{self.project_id}/segments/", {"page_size": 100}, "list segments")

    def create_segment(self, spec: dict) -> dict:
        return self._call("POST", f"/projects/{self.project_id}/segments/", body=segment_payload(spec),
                          note=f"create segment {spec['name']}",
                          dry_result={"id": f"<{spec['name']}.id>", "name": spec["name"]}) or {}

    def list_feature_segments(self, environment_id: Any, feature_id: Any) -> list[dict]:
        return self._list("/features/feature-segments/", {"environment": environment_id, "feature": feature_id},
                          "list segment overrides")

    def create_segment_override(self, env_key: str, feature_id: Any, segment_id: Any, enabled: bool,
                                value: Any = None, note: str = "") -> dict:
        body = {
            "enabled": bool(enabled),
            "feature_state_value": feature_state_value(value),
            "feature_segment": {"segment": segment_id},
        }
        return self._call("POST", f"/environments/{env_key}/features/{feature_id}/create-segment-override/",
                          body=body, note=note or "create segment override") or {}

    # ------------------------------------------------------------ feature states
    def list_featurestates(self, env_key: str, feature_name: str | None = None,
                           feature_id: Any = None) -> list[dict]:
        params: dict[str, Any] = {}
        if feature_name:
            params["feature_name"] = feature_name
        if feature_id is not None:
            params["feature"] = feature_id
        return self._list(f"/environments/{env_key}/featurestates/", params, f"find feature state {feature_name or feature_id}")

    def environment_featurestate(self, env_key: str, name: str) -> dict | None:
        """The environment-level state: no identity, no feature_segment."""
        rows = self.list_featurestates(env_key, feature_name=name)
        if self.dry_run:
            return {"id": f"<{name}.featurestate_id>"}
        for fs in rows:
            if fs.get("identity") in (None, "") and fs.get("feature_segment") in (None, ""):
                return fs
        return None

    def patch_featurestate(self, env_key: str, featurestate_id: Any, body: dict, note: str = "") -> dict:
        return self._call("PATCH", f"/environments/{env_key}/featurestates/{featurestate_id}/", body=body,
                          note=note or "update feature state") or {}

    def set_state(self, env_key: str, name: str, enabled: bool | None = None, value: Any = UNSET) -> dict:
        fs = self.environment_featurestate(env_key, name)
        if not fs:
            raise FlagsmithError(f"no environment-level feature state for {name!r} (does the feature exist?)")
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if value is not UNSET:
            body["feature_state_value"] = value
        parts = []
        if enabled is not None:
            parts.append("on" if enabled else "off")
        if value is not UNSET:
            parts.append(f"value={value!r}")
        return self.patch_featurestate(env_key, fs["id"], body, note=f"set {name} {' '.join(parts)}")

    def toggle_flag(self, env_key: str, name: str, enabled: bool) -> dict:
        return self.set_state(env_key, name, enabled=enabled)

    def set_value(self, env_key: str, name: str, value: Any) -> dict:
        return self.set_state(env_key, name, value=value)

    # ------------------------------------------------------------ idempotent apply of flags.yaml
    def apply_spec(self, spec: dict, env_key: str) -> list[ApplyResult]:
        results: list[ApplyResult] = []
        env = self.get_environment(env_key)
        env_id = env.get("id")
        if env.get("use_v2_feature_versioning"):
            results.append(ApplyResult("environment", env_key, "warning",
                                       "Feature Versioning v2 is ON: featurestate write endpoints will not work"))

        features = {f.get("name"): f for f in self.list_features()}
        for fspec in spec.get("features") or []:
            name = fspec["name"]
            existing = features.get(name)
            if existing is None:
                created = self.create_feature(fspec)
                features[name] = created or {"name": name}
                results.append(ApplyResult("feature", name, "planned" if self.dry_run else "created",
                                           fspec.get("type", "FLAG")))
                continue
            action = "exists"
            desc = fspec.get("description")
            if desc and (existing.get("description") or "") != desc:
                self.update_feature_description(existing["id"], name, desc)
                action = "updated"
            results.append(ApplyResult("feature", name, action, fspec.get("type", "FLAG")))
            if fspec.get("type") == "MULTIVARIATE":
                have = {str(o.get("string_value")) for o in self.list_mv_options(existing["id"])}
                for v in fspec.get("variants") or []:
                    if str(v["value"]) not in have:
                        self.create_mv_option(existing["id"], v["value"], v.get("percentage", 0))
                        results.append(ApplyResult("mv-option", f"{name}={v['value']}",
                                                   "planned" if self.dry_run else "created", f"{v.get('percentage', 0)}%"))

        for fspec in spec.get("features") or []:
            parent = fspec.get("depends_on")
            if not parent:
                continue
            child_id = (features.get(fspec["name"]) or {}).get("id")
            parent_id = (features.get(parent) or {}).get("id")
            label = f"{fspec['name']} -> {parent}"
            if child_id is None or parent_id is None:
                results.append(ApplyResult("dependency", label, "skipped", "feature id unknown"))
                continue
            try:
                deps = self.list_dependencies(env_key, child_id)
                if any(_dep_prereq_id(d) == parent_id for d in deps):
                    results.append(ApplyResult("dependency", label, "exists"))
                    continue
                self.add_dependency(env_key, child_id, parent_id, note=f"{fspec['name']} depends on {parent}")
                results.append(ApplyResult("dependency", label, "planned" if self.dry_run else "created"))
            except FlagsmithError as e:
                results.append(ApplyResult("dependency", label, "warning",
                                           f"flag dependencies unavailable ({e.status}); the SDK enforces the parent"))

        segments = {s.get("name"): s for s in self.list_segments()}
        for sspec in spec.get("segments") or []:
            sname = sspec["name"]
            seg = segments.get(sname)
            if seg is None:
                seg = self.create_segment(sspec) or {"name": sname}
                segments[sname] = seg
                results.append(ApplyResult("segment", sname, "planned" if self.dry_run else "created"))
            else:
                results.append(ApplyResult("segment", sname, "exists"))
            for ov in sspec.get("overrides") or []:
                fname = ov["feature"]
                fid = (features.get(fname) or {}).get("id")
                label = f"{sname} -> {fname}={'on' if ov.get('enabled', True) else 'off'}"
                if fid is None or seg.get("id") is None:
                    results.append(ApplyResult("override", label, "skipped", "feature or segment id unknown"))
                    continue
                current = [fs for fs in self.list_feature_segments(env_id, fid) if fs.get("segment") == seg["id"]]
                if current:
                    results.append(ApplyResult("override", label, "exists"))
                    continue
                self.create_segment_override(env_key, fid, seg["id"], ov.get("enabled", True), ov.get("value"),
                                             note=f"override {fname} for segment {sname}")
                results.append(ApplyResult("override", label, "planned" if self.dry_run else "created"))
        return results

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------------------------------------------------------- payload builders
def feature_payload(spec: dict) -> dict:
    ftype = spec.get("type", "FLAG")
    body: dict[str, Any] = {
        "name": spec["name"],
        "type": "MULTIVARIATE" if ftype == "MULTIVARIATE" else "STANDARD",
        "default_enabled": bool(spec.get("default_enabled", False)),
        "description": spec.get("description") or "",
    }
    if ftype in ("CONFIG", "MULTIVARIATE") and spec.get("default_value") is not None:
        body["initial_value"] = str(spec["default_value"])
    if ftype == "MULTIVARIATE":
        body["multivariate_options"] = [
            {"type": "unicode", "string_value": str(v["value"]),
             "default_percentage_allocation": float(v.get("percentage", 0))}
            for v in spec.get("variants") or []
        ]
    return body


def segment_payload(spec: dict) -> dict:
    """Flagsmith's UI shape: an ALL rule holding one ANY sub-rule per condition (conditions are ANDed)."""
    subrules = []
    for rule in spec.get("rules") or []:
        subrules.append({
            "type": "ANY",
            "rules": [],
            "conditions": [{
                "operator": rule["operator"],
                "property": rule.get("property") or "",
                "value": str(rule.get("value", "")),
            }],
        })
    return {
        "name": spec["name"],
        "description": spec.get("description") or "",
        "rules": [{"type": "ALL", "rules": subrules, "conditions": []}],
    }


def feature_state_value(value: Any) -> dict:
    if isinstance(value, bool):
        return {"type": "bool", "boolean_value": value}
    if isinstance(value, int):
        return {"type": "int", "integer_value": value}
    return {"type": "unicode", "string_value": None if value is None else str(value)}


def _dep_prereq_id(d: dict) -> Any:
    p = d.get("prerequisite")
    if isinstance(p, dict):
        return p.get("id")
    return p
