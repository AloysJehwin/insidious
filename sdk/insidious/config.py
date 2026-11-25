"""Load insidious.yaml and flagsmith/flags.yaml.

Search order (first existing file wins). Every INSIDIOUS_* env var falls back to its legacy FESTIVEKIT_* name.

insidious.yaml: $INSIDIOUS_CONFIG, /config/insidious.yaml, ./insidious.yaml, any parent of cwd,
                 <repo root>/insidious.yaml (when the SDK runs from a source checkout); at each location the
                 legacy name festivekit.yaml is tried after insidious.yaml.
flags.yaml:      $INSIDIOUS_FLAGS, /config/flags.yaml, ./flagsmith/flags.yaml, ./flags.yaml, parents of cwd,
                 <repo root>/flagsmith/flags.yaml, then a built-in copy so offline mode always works.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Iterable

import yaml

CONFIG_NAMES = ("insidious.yaml", "festivekit.yaml")  # new name first, legacy name as fallback


def env(name: str, default: str | None = None) -> str | None:
    """Read INSIDIOUS_<X>, falling back to the legacy FESTIVEKIT_<X> when the new name is unset."""
    val = os.environ.get(name)
    if val is None and name.startswith("INSIDIOUS_"):
        val = os.environ.get("FESTIVEKIT_" + name[len("INSIDIOUS_"):])
    return default if val is None else val

log = logging.getLogger("insidious.config")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Built-in mirror of flagsmith/flags.yaml. Used only when no flags file can be found.
BUILTIN_FLAGS: dict[str, Any] = {
    "features": [
        {"name": "heavy_features", "type": "FLAG", "default_enabled": True},
        {"name": "recommendations", "type": "FLAG", "default_enabled": True, "depends_on": "heavy_features"},
        {"name": "hd_images", "type": "FLAG", "default_enabled": True, "depends_on": "heavy_features"},
        {"name": "live_reviews", "type": "FLAG", "default_enabled": True, "depends_on": "heavy_features"},
        {
            "name": "checkout_flow",
            "type": "MULTIVARIATE",
            "default_enabled": True,
            "default_value": "full",
            "variants": [{"value": "full", "percentage": 50}, {"value": "lite", "percentage": 50}],
        },
        {"name": "payment_gateway", "type": "CONFIG", "default_enabled": True, "default_value": "razorpay"},
        {
            "name": "ops_voice",
            "type": "CONFIG",
            "default_enabled": True,
            "default_value": '{"speaker": "", "pace": 1.0, "language": "hi-IN"}',
        },
        {"name": "festive_collection", "type": "FLAG", "default_enabled": False},
        {"name": "read_from_replica", "type": "FLAG", "default_enabled": False},
        {"name": "db_read_url", "type": "CONFIG", "default_enabled": True, "default_value": ""},
        {
            "name": "db_pool",
            "type": "CONFIG",
            "default_enabled": True,
            "default_value": '{"pool_size":5,"max_overflow":0,"timeout_s":2}',
        },
        {
            "name": "agent_config",
            "type": "CONFIG",
            "default_enabled": True,
            "default_value": '{"provider":"anthropic","model":"claude-sonnet-5-5","autopilot":false,"max_actions":4}',
        },
        {"name": "ai_agent", "type": "FLAG", "default_enabled": True},
    ],
    "segments": [
        {
            "name": "high_value_cart",
            "rules": [{"property": "cart_value", "operator": "GREATER_THAN", "value": "2000"}],
            "overrides": [{"feature": "heavy_features", "enabled": True}],
        },
        {
            "name": "festive_collection_rollout",
            "rules": [{"property": "", "operator": "PERCENTAGE_SPLIT", "value": "10"}],
            "overrides": [{"feature": "festive_collection", "enabled": True}],
        },
    ],
}

BUILTIN_CONFIG: dict[str, Any] = {
    "app": {"name": "jeffy", "namespace": "store", "deployment": "store-api"},
    "slos": [
        {"route": "/checkout", "p95_ms": 800, "error_rate": 0.02},
        {"route": "/", "p95_ms": 1200, "error_rate": 0.05},
    ],
    "degradable": {
        "parent_flag": "heavy_features",
        "features": [
            {"flag": "recommendations", "fallback": "cached_top_sellers"},
            {"flag": "hd_images", "fallback": "low_res_images"},
            {"flag": "live_reviews", "fallback": "cached_reviews"},
        ],
    },
}


def _parents_of_cwd(rel: str) -> Iterable[Path]:
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        yield p / rel


def _first_existing(candidates: Iterable[Path | str | None]) -> Path | None:
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def find_config_path() -> Path | None:
    return _first_existing(
        [
            env("INSIDIOUS_CONFIG"),
            *(f"/config/{n}" for n in CONFIG_NAMES),
            *(c for d in _parents_of_cwd("") for c in (d / n for n in CONFIG_NAMES)),
            *(_REPO_ROOT / n for n in CONFIG_NAMES),
        ]
    )


def find_flags_path() -> Path | None:
    return _first_existing(
        [
            env("INSIDIOUS_FLAGS"),
            "/config/flags.yaml",
            *_parents_of_cwd("flagsmith/flags.yaml"),
            Path.cwd() / "flags.yaml",
            _REPO_ROOT / "flagsmith" / "flags.yaml",
        ]
    )


def _load_yaml(path: Path | None, builtin: dict[str, Any], what: str) -> dict[str, Any]:
    if path is None:
        log.warning('{"svc":"insidious","event":"config_missing","what":"%s","using":"builtin"}', what)
        return copy.deepcopy(builtin)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("top-level YAML must be a mapping")
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning(
            '{"svc":"insidious","event":"config_unreadable","what":"%s","path":"%s","error":"%s"}', what, path, exc
        )
        return copy.deepcopy(builtin)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return insidious.yaml as a dict (built-in defaults if it cannot be found)."""
    return _load_yaml(Path(path) if path else find_config_path(), BUILTIN_CONFIG, "insidious.yaml")


def load_flags(path: str | Path | None = None) -> dict[str, Any]:
    """Return flagsmith/flags.yaml as a dict (built-in copy if it cannot be found)."""
    return _load_yaml(Path(path) if path else find_flags_path(), BUILTIN_FLAGS, "flags.yaml")
