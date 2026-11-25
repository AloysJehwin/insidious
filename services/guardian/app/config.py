"""Configuration: env vars + insidious.yaml (CONTRACTS.md conventions)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLACEHOLDER_HINTS = ("xxxx", "change-me")


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    v = value.strip().lower()
    return any(h in v for h in PLACEHOLDER_HINTS)


def getenv(name: str, default: str | None = None) -> str | None:
    """os.getenv that falls back from INSIDIOUS_<X> to the legacy FESTIVEKIT_<X> when the new name is unset."""
    val = os.getenv(name)
    if val is None and name.startswith("INSIDIOUS_"):
        val = os.getenv("FESTIVEKIT_" + name[len("INSIDIOUS_"):])
    return default if val is None else val


CONFIG_NAMES = ("insidious.yaml", "festivekit.yaml")  # new name first, legacy name as fallback


def _config_candidates() -> list[Path]:
    out: list[Path] = []
    if getenv("INSIDIOUS_CONFIG"):
        out.append(Path(getenv("INSIDIOUS_CONFIG")))
    out.extend(Path("/config") / n for n in CONFIG_NAMES)
    out.extend(Path.cwd() / n for n in CONFIG_NAMES)
    # repo root when running from a checkout: services/guardian/app/config.py -> ../../../insidious.yaml
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:  # inside a container (/app/app/config.py) there is no repo root
        out.extend(parents[3] / n for n in CONFIG_NAMES)
    return out


def load_yaml_config() -> dict[str, Any]:
    for p in _config_candidates():
        try:
            if p.is_file():
                return yaml.safe_load(p.read_text()) or {}
        except Exception:  # noqa: BLE001 - fall through to next candidate
            continue
    return {}


def parse_duration_seconds(value: str | int | float | None, default: float = 30.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*", str(value))
    if not m:
        return default
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n


@dataclass
class SLO:
    route: str
    p95_ms: float
    error_rate: float


@dataclass
class Settings:
    app_name: str = "jeffy"
    namespace: str = "store"
    deployment: str = "store-api"
    slos: list[SLO] = field(default_factory=lambda: [SLO("/checkout", 800, 0.02)])
    interval_seconds: float = 5.0
    breach_samples: int = 3
    recover_samples: int = 3
    rate_window: str = "30s"
    rule_name: str = "insidious-slo"
    prometheus_url: str = "http://prometheus:9090"
    store_url: str = "http://store-api:8000"
    reports_dir: Path = Path("./reports")
    armed: bool = True
    api_alias: bool = True
    nudgebee_url: str = ""
    nudgebee_token: str = ""
    nudgebee_account_id: str = ""
    nudgebee_event_webhook_url: str = ""
    nudgebee_wf_incident_id: str = ""
    nudgebee_dry_run: bool = False
    incident_workflow_name: str = "sale-incident-guard"
    k8s_enabled: bool = True
    http_timeout: float = 4.0
    # Database SLO (insidious.yaml `database.slo`): pool utilisation >= target OR wait p95 > target
    db_slo_enabled: bool = True
    db_target_utilisation: float = 0.9
    db_target_wait_p95_s: float = 0.25
    db_rule_name: str = "insidious-db"
    # Claude ops agent: every fired incident is POSTed to AGENT_URL/api/incident (best effort)
    agent_url: str = ""
    internal_token: str = ""
    agent_timeout: float = 2.0

    @classmethod
    def from_env(cls, cfg: dict[str, Any] | None = None) -> "Settings":
        cfg = load_yaml_config() if cfg is None else cfg
        app = cfg.get("app") or {}
        g = cfg.get("guardian") or {}
        nb = cfg.get("nudgebee") or {}
        dbc = cfg.get("database") or {}
        db_slo = dbc.get("slo") or {}
        slos = [
            SLO(str(s["route"]), float(s.get("p95_ms", 800)), float(s.get("error_rate", 0.02)))
            for s in (cfg.get("slos") or [])
            if s.get("route")
        ] or [SLO("/checkout", 800, 0.02)]
        return cls(
            app_name=str(app.get("name", "jeffy")),
            namespace=str(app.get("namespace", "store")),
            deployment=str(app.get("deployment", "store-api")),
            slos=slos,
            interval_seconds=float(os.getenv("GUARDIAN_INTERVAL_SECONDS", g.get("interval_seconds", 5))),
            breach_samples=int(g.get("breach_samples", 3)),
            recover_samples=int(g.get("recover_samples", 3)),
            rate_window=str(g.get("rate_window", "30s")),
            rule_name=str(nb.get("rule_name", "insidious-slo")),
            prometheus_url=os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/"),
            store_url=os.getenv("STORE_URL", "http://store-api:8000").rstrip("/"),
            reports_dir=Path(getenv("INSIDIOUS_REPORTS_DIR", "./reports")),
            armed=env_flag("GUARDIAN_ARMED", "1"),
            api_alias=env_flag("GUARDIAN_API_ALIAS", "1"),
            nudgebee_url=os.getenv("NUDGEBEE_URL", "").rstrip("/"),
            nudgebee_token=os.getenv("NUDGEBEE_API_TOKEN", ""),
            nudgebee_account_id=os.getenv("NUDGEBEE_ACCOUNT_ID", ""),
            nudgebee_event_webhook_url=os.getenv("NUDGEBEE_EVENT_WEBHOOK_URL", ""),
            nudgebee_wf_incident_id=os.getenv(str(nb.get("workflows", {}).get("incident_env", "NUDGEBEE_WF_INCIDENT_ID")), ""),
            nudgebee_dry_run=env_flag("NUDGEBEE_DRY_RUN", "0"),
            incident_workflow_name=os.getenv("NUDGEBEE_WF_INCIDENT_NAME", "sale-incident-guard"),
            k8s_enabled=env_flag("GUARDIAN_K8S", "1"),
            db_slo_enabled=env_flag("GUARDIAN_DB_SLO", "1"),
            db_target_utilisation=float(db_slo.get("pool_utilisation", 0.9)),
            db_target_wait_p95_s=float(db_slo.get("wait_p95_s", 0.25)),
            agent_url=os.getenv("AGENT_URL", "").strip().rstrip("/"),
            internal_token=getenv("INSIDIOUS_INTERNAL_TOKEN", ""),
        )

    @property
    def rate_window_seconds(self) -> float:
        return parse_duration_seconds(self.rate_window)
