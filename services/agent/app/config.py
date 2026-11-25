"""Configuration: env vars + insidious.yaml (CONTRACTS.md conventions)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLACEHOLDER_HINTS = ("xxxx", "change-me")

DEFAULT_GUARDRAILS: dict[str, Any] = {
    "max_replicas": 5,
    "allowed_namespaces": ["store"],
    "allowed_db_instance_classes": ["db.t4g.micro", "db.t4g.small", "db.t4g.medium"],
    "max_memory_limit": "2Gi",
    "max_cpu_limit": "2",
    # Accepted RDS identifier prefixes; "festivekit-" keeps the pre-rename DBs (festivekit-primary, ...) manageable.
    "rds_id_prefix": ["insidious-", "festivekit-"],
    # Extra defaults (not in CONTRACTS, conservative): DB pool bounds and flags the AI may never touch.
    "max_db_pool_size": 40,
    "protected_flags": ["ai_agent", "agent_config"],
}


CONFIG_NAMES = ("insidious.yaml", "festivekit.yaml")  # new name first, legacy name as fallback


def getenv(name: str, default: str | None = None) -> str | None:
    """os.getenv that falls back from INSIDIOUS_<X> to the legacy FESTIVEKIT_<X> when the new name is unset."""
    val = os.getenv(name)
    if val is None and name.startswith("INSIDIOUS_"):
        val = os.getenv("FESTIVEKIT_" + name[len("INSIDIOUS_"):])
    return default if val is None else val


def env_flag(name: str, default: str = "0") -> bool:
    return getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    v = value.strip().lower()
    return any(h in v for h in PLACEHOLDER_HINTS)


def configured(value: str | None) -> str:
    """The value, or "" when it is unset or still a .env.example placeholder."""
    return "" if is_placeholder(value) else str(value).strip()


def _candidates(env_name: str, default_dir: str, filenames: tuple[str, ...], subdir: str = "") -> list[Path]:
    out: list[Path] = []
    if getenv(env_name):
        out.append(Path(getenv(env_name)))
    out.extend(Path(default_dir) / f for f in filenames)
    out.extend(Path.cwd() / f for f in filenames)
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:  # repo checkout: services/agent/app/config.py -> repo root
        out.extend(parents[3] / subdir / f if subdir else parents[3] / f for f in filenames)
    return out


def _load_yaml(paths: list[Path]) -> dict[str, Any]:
    for p in paths:
        try:
            if p.is_file():
                return yaml.safe_load(p.read_text()) or {}
        except Exception:  # noqa: BLE001 - fall through to next candidate
            continue
    return {}


def load_yaml_config() -> dict[str, Any]:
    return _load_yaml(_candidates("INSIDIOUS_CONFIG", "/config", CONFIG_NAMES))


def load_flags_doc() -> dict[str, Any]:
    return _load_yaml(_candidates("INSIDIOUS_FLAGS", "/config", ("flags.yaml",), "flagsmith"))


@dataclass
class Settings:
    namespace: str = "store"
    deployment: str = "store-api"
    guardian_url: str = "http://guardian:8002"
    ops_bot_url: str = "http://ops-bot:8003"
    prometheus_url: str = "http://prometheus:9090"
    store_url: str = "http://store-api:8000"
    internal_token: str = ""
    data_dir: Path = Path("./data")
    reports_dir: Path = Path("./reports")
    mock: bool = False
    dry_run: bool = False
    # Claude
    model: str = "claude-sonnet-5-5"
    provider: str = "anthropic"
    anthropic_api_key: str = ""
    aws_region: str = "ap-south-1"
    bedrock_api: str = "mantle"  # mantle (Messages API on Bedrock) | invoke (legacy InvokeModel)
    bedrock_model: str = ""
    # Data / AWS
    database_url: str = ""
    rds_id: str = ""
    rds_poll_seconds: float = 30.0
    rds_watch_seconds: float = 45 * 60.0
    # NudgeBee
    nudgebee_url: str = ""
    nudgebee_token: str = ""
    nudgebee_account_id: str = ""
    nudgebee_dry_run: bool = False
    nudgebee_rca: bool = True
    rca_timeout_s: float = 60.0
    # Flagsmith
    flagsmith_server_key: str = ""
    flagsmith_api_url: str = ""
    flagsmith_refresh_seconds: float = 30.0
    flagsmith_environment_key: str = ""
    flagsmith_org_api_key: str = ""
    flagsmith_admin_url: str = "https://api.flagsmith.com/api/v1"
    # Kubernetes
    k8s_enabled: bool = True
    # Misc
    http_timeout: float = 5.0
    guardrails: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_GUARDRAILS))
    sale_replicas: int = 3
    flags_doc: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, cfg: dict[str, Any] | None = None, flags_doc: dict[str, Any] | None = None) -> "Settings":
        cfg = load_yaml_config() if cfg is None else cfg
        flags_doc = load_flags_doc() if flags_doc is None else flags_doc
        app = cfg.get("app") or {}
        agent = cfg.get("agent") or {}
        guardrails = dict(DEFAULT_GUARDRAILS)
        guardrails.update({k: v for k, v in (agent.get("guardrails") or {}).items() if v is not None})
        scale = (cfg.get("nudgebee") or {}).get("scale") or {}
        return cls(
            namespace=str(app.get("namespace", "store")),
            deployment=str(app.get("deployment", "store-api")),
            guardian_url=os.getenv("GUARDIAN_URL", "http://guardian:8002").rstrip("/"),
            ops_bot_url=os.getenv("OPS_BOT_URL", "http://ops-bot:8003").rstrip("/"),
            prometheus_url=os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/"),
            store_url=os.getenv("STORE_URL", "http://store-api:8000").rstrip("/"),
            internal_token=getenv("INSIDIOUS_INTERNAL_TOKEN", ""),
            data_dir=Path(os.getenv("DATA_DIR", "./data")),
            reports_dir=Path(getenv("INSIDIOUS_REPORTS_DIR", "./reports")),
            mock=env_flag("AGENT_MOCK", "0"),
            dry_run=env_flag("AGENT_DRY_RUN", "0"),
            model=os.getenv("AGENT_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5",
            provider=(os.getenv("AGENT_PROVIDER", "anthropic").strip() or "anthropic").lower(),
            anthropic_api_key=configured(os.getenv("ANTHROPIC_API_KEY")),
            aws_region=os.getenv("AWS_REGION", "ap-south-1").strip() or "ap-south-1",
            bedrock_api=(os.getenv("AGENT_BEDROCK_API", "mantle").strip() or "mantle").lower(),
            bedrock_model=os.getenv("AGENT_BEDROCK_MODEL", "").strip(),
            database_url=os.getenv("DATABASE_URL", ""),
            rds_id=os.getenv("DATABASE_RDS_ID", "").strip(),
            rds_poll_seconds=float(os.getenv("AGENT_RDS_POLL_SECONDS", "30")),
            rds_watch_seconds=float(os.getenv("AGENT_RDS_WATCH_SECONDS", str(45 * 60))),
            nudgebee_url=os.getenv("NUDGEBEE_URL", "").rstrip("/"),
            nudgebee_token=configured(os.getenv("NUDGEBEE_API_TOKEN")),
            nudgebee_account_id=configured(os.getenv("NUDGEBEE_ACCOUNT_ID")),
            nudgebee_dry_run=env_flag("NUDGEBEE_DRY_RUN", "0"),
            nudgebee_rca=env_flag("AGENT_NUDGEBEE_RCA", "1"),
            rca_timeout_s=float(os.getenv("AGENT_RCA_TIMEOUT_S", "60")),
            flagsmith_server_key=configured(os.getenv("FLAGSMITH_SERVER_KEY")),
            flagsmith_api_url=os.getenv("FLAGSMITH_API_URL", "").strip(),
            flagsmith_refresh_seconds=float(os.getenv("AGENT_FLAGSMITH_REFRESH_SECONDS", "30")),
            flagsmith_environment_key=configured(os.getenv("FLAGSMITH_ENVIRONMENT_KEY")),
            flagsmith_org_api_key=configured(os.getenv("FLAGSMITH_ORG_API_KEY")),
            flagsmith_admin_url=(os.getenv("FLAGSMITH_ADMIN_URL", "") or "https://api.flagsmith.com/api/v1").rstrip("/"),
            k8s_enabled=env_flag("AGENT_K8S", "1"),
            guardrails=guardrails,
            sale_replicas=int(scale.get("sale_replicas", 3) or 3),
            flags_doc=flags_doc or {},
        )
