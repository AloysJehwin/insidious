"""Action catalog + guardrails. The ONLY actions the agent may propose or execute (CONTRACTS.md v2)."""
from __future__ import annotations

import json
import re
from typing import Any

from .models import Action

ACTION_TYPES = (
    "flagsmith.set_flag",
    "flagsmith.set_value",
    "k8s.scale",
    "k8s.set_resources",
    "db.set_pool",
    "aws.rds.create_read_replica",
    "aws.rds.modify_instance_class",
    "nudgebee.run_workflow",
    "notify.founder",
)

# Rough hourly prices used in cost_en (₹/hour). Simple constants on purpose.
COST_INR_PER_HOUR = {
    "store_server": 4.0,  # one extra store-api pod / node share
    "db.t4g.micro": 1.5,
    "db.t4g.small": 3.0,
    "db.t4g.medium": 6.0,
}
CPU_CORE_INR_PER_HOUR = 4.0  # "more CPU" priced like one extra server per core

_FLAG_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_RDS_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")


class GuardrailError(ValueError):
    """An action is outside the catalog or the guardrails. The message is shown to humans."""


# ---------------------------------------------------------------- quantity parsing
_MEM_UNITS = {
    "": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12,
    "ki": 2**10, "mi": 2**20, "gi": 2**30, "ti": 2**40,
}


def parse_cpu(value: Any) -> float:
    """Kubernetes CPU quantity -> cores ("500m" -> 0.5, "2" -> 2.0)."""
    s = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)", s)
    if not m:
        raise GuardrailError(f"bad CPU quantity {value!r} (use e.g. '500m' or '1')")
    n = float(m.group(1))
    return n / 1000 if m.group(2) else n


def parse_memory(value: Any) -> int:
    """Kubernetes memory quantity -> bytes ("512Mi", "1Gi", "1G")."""
    s = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTkmgt]i?)?", s)
    if not m:
        raise GuardrailError(f"bad memory quantity {value!r} (use e.g. '512Mi' or '1Gi')")
    unit = (m.group(2) or "").lower()
    return int(float(m.group(1)) * _MEM_UNITS[unit])


def format_cpu(cores: float) -> str:
    if cores >= 1 and abs(cores - round(cores)) < 1e-9:
        return str(int(round(cores)))
    return f"{int(round(cores * 1000))}m"


def format_memory(nbytes: int) -> str:
    if nbytes % 2**30 == 0:
        return f"{nbytes // 2**30}Gi"
    return f"{int(round(nbytes / 2**20))}Mi"


# ---------------------------------------------------------------- coercion helpers
def _req(params: dict, name: str) -> Any:
    if name not in params or params[name] is None or params[name] == "":
        raise GuardrailError(f"missing parameter {name!r}")
    return params[name]


def _bool(v: Any, name: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "on", "1", "yes", "false", "off", "0", "no"):
        return v.strip().lower() in ("true", "on", "1", "yes")
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise GuardrailError(f"{name} must be true or false, got {v!r}")


def _int(v: Any, name: str, lo: int, hi: int, what: str = "") -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise GuardrailError(f"{name} must be a whole number, got {v!r}") from None
    if f != int(f):
        raise GuardrailError(f"{name} must be a whole number, got {v!r}")
    n = int(f)
    if n < lo or n > hi:
        raise GuardrailError(f"{name}={n} is outside the allowed range {lo}..{hi}{what}")
    return n


def _num(v: Any, name: str, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise GuardrailError(f"{name} must be a number, got {v!r}") from None
    if f < lo or f > hi:
        raise GuardrailError(f"{name}={f:g} is outside the allowed range {lo:g}..{hi:g}")
    return f


def _namespace(params: dict, g: dict) -> str:
    ns = str(_req(params, "namespace"))
    allowed = [str(x) for x in g.get("allowed_namespaces") or []]
    if ns not in allowed:
        raise GuardrailError(f"namespace {ns!r} is not allowed (allowed: {', '.join(allowed) or 'none'})")
    return ns


def _k8s_name(v: Any, name: str) -> str:
    s = str(v)
    if not _K8S_NAME_RE.match(s):
        raise GuardrailError(f"{name} {s!r} is not a valid Kubernetes name")
    return s


def rds_prefixes(g: dict) -> list[str]:
    """guardrails.rds_id_prefix as a list (accepts one string or a list of strings)."""
    raw = g.get("rds_id_prefix") or []
    return [str(p) for p in ([raw] if isinstance(raw, str) else raw) if p]


def _rds_id(v: Any, name: str, g: dict) -> str:
    s = str(v)
    prefixes = rds_prefixes(g)
    if not _RDS_ID_RE.match(s):
        raise GuardrailError(f"{name} {s!r} is not a valid RDS identifier")
    if prefixes and not s.startswith(tuple(prefixes)):
        raise GuardrailError(f"{name} {s!r} must start with one of {prefixes!r}")
    return s


def _instance_class(v: Any, g: dict) -> str:
    s = str(v)
    allowed = [str(x) for x in g.get("allowed_db_instance_classes") or []]
    if s not in allowed:
        raise GuardrailError(f"instance class {s!r} is not allowed (allowed: {', '.join(allowed)})")
    return s


def _flag(params: dict, g: dict) -> str:
    flag = str(_req(params, "flag"))
    if not _FLAG_RE.match(flag):
        raise GuardrailError(f"bad flag name {flag!r}")
    if flag in (g.get("protected_flags") or []):
        raise GuardrailError(f"flag {flag!r} is protected and cannot be changed by the agent")
    return flag


# ---------------------------------------------------------------- validation
def validate_action(action: Action | dict, g: dict) -> Action:
    """Return a normalised copy of the action, or raise GuardrailError with a clear message."""
    if isinstance(action, dict):
        action = Action(type=str(action.get("type", "")), params=dict(action.get("params") or {}))
    t, p = action.type, dict(action.params or {})
    if t not in ACTION_TYPES:
        raise GuardrailError(f"action {t!r} is not in the action catalog")

    if t == "flagsmith.set_flag":
        out = {"flag": _flag(p, g), "enabled": _bool(_req(p, "enabled"), "enabled")}
    elif t == "flagsmith.set_value":
        v = p.get("value")
        if v is None:
            raise GuardrailError("missing parameter 'value'")
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"))
        elif isinstance(v, bool):
            v = "true" if v else "false"
        out = {"flag": _flag(p, g), "value": str(v)}
    elif t == "k8s.scale":
        mx = int(g.get("max_replicas", 5))
        out = {
            "namespace": _namespace(p, g),
            "deployment": _k8s_name(_req(p, "deployment"), "deployment"),
            "replicas": _int(_req(p, "replicas"), "replicas", 1, mx, f" (max_replicas is {mx})"),
        }
    elif t == "k8s.set_resources":
        ns = _namespace(p, g)
        dep = _k8s_name(_req(p, "deployment"), "deployment")
        container = _k8s_name(p.get("container") or dep, "container")
        cpu_lim = parse_cpu(_req(p, "cpu_limit"))
        mem_lim = parse_memory(_req(p, "memory_limit"))
        max_cpu = parse_cpu(g.get("max_cpu_limit", "2"))
        max_mem = parse_memory(g.get("max_memory_limit", "2Gi"))
        if cpu_lim <= 0 or cpu_lim > max_cpu:
            raise GuardrailError(f"cpu_limit {p['cpu_limit']} is above the max_cpu_limit {g.get('max_cpu_limit')}")
        if mem_lim <= 0 or mem_lim > max_mem:
            raise GuardrailError(
                f"memory_limit {p['memory_limit']} is above the max_memory_limit {g.get('max_memory_limit')}")
        out = {"namespace": ns, "deployment": dep, "container": container,
               "cpu_limit": format_cpu(cpu_lim), "memory_limit": format_memory(mem_lim)}
        if p.get("cpu_request"):
            cr = parse_cpu(p["cpu_request"])
            if cr > cpu_lim:
                raise GuardrailError("cpu_request must not exceed cpu_limit")
            out["cpu_request"] = format_cpu(cr)
        if p.get("memory_request"):
            mr = parse_memory(p["memory_request"])
            if mr > mem_lim:
                raise GuardrailError("memory_request must not exceed memory_limit")
            out["memory_request"] = format_memory(mr)
    elif t == "db.set_pool":
        mx = int(g.get("max_db_pool_size", 40))
        out = {
            "pool_size": _int(_req(p, "pool_size"), "pool_size", 1, mx),
            "max_overflow": _int(p.get("max_overflow", 0), "max_overflow", 0, mx),
            "timeout_s": _num(p.get("timeout_s", 2), "timeout_s", 0.1, 60),
        }
    elif t == "aws.rds.create_read_replica":
        src = _rds_id(_req(p, "source_db_id"), "source_db_id", g)
        rep = _rds_id(_req(p, "replica_id"), "replica_id", g)
        if rep == src:
            raise GuardrailError("replica_id must differ from source_db_id")
        out = {"source_db_id": src, "replica_id": rep, "instance_class": _instance_class(_req(p, "instance_class"), g)}
    elif t == "aws.rds.modify_instance_class":
        out = {"db_id": _rds_id(_req(p, "db_id"), "db_id", g),
               "instance_class": _instance_class(_req(p, "instance_class"), g)}
    elif t == "nudgebee.run_workflow":
        wf = str(_req(p, "workflow_id")).strip()
        inputs = p.get("inputs") or {}
        if isinstance(inputs, str):
            try:
                inputs = json.loads(inputs) if inputs.strip() else {}
            except ValueError:
                raise GuardrailError("inputs must be a JSON object") from None
        if not isinstance(inputs, dict):
            raise GuardrailError("inputs must be a JSON object")
        out = {"workflow_id": wf, "inputs": inputs}
    else:  # notify.founder
        text = " ".join(str(_req(p, "text_en")).split())
        if len(text) > 1000:
            raise GuardrailError("text_en is longer than 1000 characters")
        out = {"text_en": text}
    return Action(type=t, params=out)


def validate_actions(actions: list[Action], g: dict, max_actions: int | None = None) -> list[Action]:
    if max_actions is not None and len(actions) > max_actions:
        raise GuardrailError(f"too many actions ({len(actions)} > max_actions {max_actions})")
    return [validate_action(a, g) for a in actions]


def describe_catalog(g: dict) -> str:
    """Human/LLM-readable catalog + guardrails for the system prompt."""
    return f"""Action catalog (use ONLY these `type` values; params exactly as listed):
- flagsmith.set_flag {{flag, enabled}}: turn a feature flag on/off for everyone (e.g. heavy_features=false pauses product suggestions, HD images and live reviews; read_from_replica=true sends catalog reads to the database copy).
- flagsmith.set_value {{flag, value}}: set a remote-config value (string; JSON as a string).
- k8s.scale {{namespace, deployment, replicas}}: set the number of store servers. 1 <= replicas <= {g.get('max_replicas')}.
- k8s.set_resources {{namespace, deployment, container, cpu_limit, memory_limit, cpu_request?, memory_request?}}: give each store server more CPU / memory. cpu_limit <= "{g.get('max_cpu_limit')}", memory_limit <= "{g.get('max_memory_limit')}".
- db.set_pool {{pool_size, max_overflow, timeout_s}}: resize the store's database connection pool live (pool_size <= {g.get('max_db_pool_size')}).
- aws.rds.create_read_replica {{source_db_id, replica_id, instance_class}}: start a read-only copy of the database (takes ~10-15 minutes; the agent switches reads to it automatically when ready).
- aws.rds.modify_instance_class {{db_id, instance_class}}: move the database to a bigger machine (brief restart).
- nudgebee.run_workflow {{workflow_id, inputs}}: run a NudgeBee workflow (inputs is a JSON object encoded as a string).
- notify.founder {{text_en}}: send the founder a message.
Guardrails: namespaces {g.get('allowed_namespaces')}; DB instance classes {g.get('allowed_db_instance_classes')}; RDS identifiers must start with one of {rds_prefixes(g)}; never change flags {g.get('protected_flags')}.
Approximate costs: one extra store server ~ ₹{COST_INR_PER_HOUR['store_server']:g}/hour; db.t4g.micro ~ ₹{COST_INR_PER_HOUR['db.t4g.micro']:g}/hour; db.t4g.small ~ ₹{COST_INR_PER_HOUR['db.t4g.small']:g}/hour; db.t4g.medium ~ ₹{COST_INR_PER_HOUR['db.t4g.medium']:g}/hour; one extra CPU core ~ ₹{CPU_CORE_INR_PER_HOUR:g}/hour; flag and pool changes are free."""
