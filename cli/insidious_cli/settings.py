"""Repo discovery, .env loading and config files (insidious.yaml, flagsmith/flags.yaml)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PLACEHOLDER_HINTS = ("xxxx", "change-me", "your-name")


CONFIG_NAMES = ("insidious.yaml", "festivekit.yaml")  # new name first, legacy name as fallback
LEGACY_PREFIX = "FESTIVEKIT_"


def getenv(name: str, default: str | None = None) -> str | None:
    """os.getenv that falls back from INSIDIOUS_<X> to the legacy FESTIVEKIT_<X> when the new name is unset."""
    val = os.getenv(name)
    if val is None and name.startswith("INSIDIOUS_"):
        val = os.getenv(LEGACY_PREFIX + name[len("INSIDIOUS_"):])
    return default if val is None else val


def config_file(d: Path) -> Path | None:
    """insidious.yaml in ``d`` (or the legacy festivekit.yaml), else None."""
    for n in CONFIG_NAMES:
        if (d / n).is_file():
            return d / n
    return None


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from cwd looking for insidious.yaml; fall back to this package's checkout location."""
    if getenv("INSIDIOUS_ROOT"):
        return Path(getenv("INSIDIOUS_ROOT")).resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if config_file(d):
            return d
    pkg_root = Path(__file__).resolve().parents[2]
    if config_file(pkg_root):
        return pkg_root
    return here


def load_env(root: Path) -> None:
    if env_flag("INSIDIOUS_NO_DOTENV"):
        return
    env = root / ".env"
    if env.is_file():
        load_dotenv(env, override=False)


def is_placeholder(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    v = value.strip().lower()
    return any(h in v for h in PLACEHOLDER_HINTS)


def env_value(name: str) -> str | None:
    """The env var's value, or None if unset/empty/placeholder."""
    v = getenv(name)
    return None if is_placeholder(v) else v


def env_flag(name: str, default: str = "0") -> bool:
    return getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def url_env(name: str, default: str) -> str:
    return (getenv(name) or default).rstrip("/")


@lru_cache(maxsize=None)
def _read_yaml(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def insidious_config(root: Path) -> dict[str, Any]:
    path = getenv("INSIDIOUS_CONFIG") or ""
    if not path or not Path(path).is_file():
        path = str(config_file(root) or root / "insidious.yaml")
    return _read_yaml(path)


def flags_spec(root: Path) -> dict[str, Any]:
    path = getenv("INSIDIOUS_FLAGS")
    if not path or not Path(path).is_file():
        path = str(root / "flagsmith" / "flags.yaml")
    return _read_yaml(path)


def reports_dir(root: Path) -> Path:
    return Path(getenv("INSIDIOUS_REPORTS_DIR") or (root / "reports"))
