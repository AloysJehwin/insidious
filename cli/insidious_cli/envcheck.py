"""Validate the environment against .env.example. Never prints secret values."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .settings import is_placeholder

SECTION_RE = re.compile(r"^#\s*-+\s*(.+?)\s*-+\s*$")
VAR_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

# Variables that may legitimately be empty (filled in later, or only needed for optional features).
OPTIONAL = {"NUDGEBEE_WF_PRESALE_ID", "NUDGEBEE_WF_INCIDENT_ID", "NUDGEBEE_WF_POSTSALE_ID"}
# Toggles: any value is fine, including the default.
TOGGLES = {"SARVAM_MOCK", "NUDGEBEE_DRY_RUN", "WHATSAPP_DRY_RUN"}
# Credentials and identifiers: the example value is never a usable default.
NEEDS_REAL_RE = re.compile(r"(KEY|TOKEN|SECRET|_ID$|PHONE|ACCOUNT)")


@dataclass
class EnvVar:
    section: str
    name: str
    example: str


@dataclass
class EnvResult:
    section: str
    name: str
    status: str  # set | default | placeholder | missing | optional
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("set", "default", "optional")


def parse_env_example(path: Path) -> list[EnvVar]:
    out: list[EnvVar] = []
    section = "General"
    for raw in path.read_text().splitlines():
        line = raw.strip()
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        m = VAR_RE.match(line)
        if m:
            value = m.group(2).split(" #", 1)[0].strip()
            out.append(EnvVar(section, m.group(1), value))
    return out


def check_env(path: Path, environ: dict[str, str] | None = None) -> list[EnvResult]:
    env = os.environ if environ is None else environ
    results: list[EnvResult] = []
    for var in parse_env_example(path):
        val = env.get(var.name)
        if val is None and var.name.startswith("INSIDIOUS_"):  # legacy FESTIVEKIT_* name still accepted
            val = env.get("FESTIVEKIT_" + var.name[len("INSIDIOUS_"):])
        example_is_placeholder = is_placeholder(var.example)
        needs_real = bool(NEEDS_REAL_RE.search(var.name)) and var.name != "WHATSAPP_VERIFY_TOKEN"
        if var.name in TOGGLES:
            results.append(EnvResult(var.section, var.name, "set" if val is not None else "default",
                                     f"= {val if val is not None else var.example}"))
        elif val is None or val.strip() == "":
            if var.name in OPTIONAL:
                results.append(EnvResult(var.section, var.name, "optional", "not set yet"))
            elif var.example and not example_is_placeholder and not needs_real:
                results.append(EnvResult(var.section, var.name, "default", "using example default"))
            else:
                results.append(EnvResult(var.section, var.name, "missing", "set it in .env"))
        elif is_placeholder(val) or ((example_is_placeholder or needs_real) and val.strip() == var.example):
            results.append(EnvResult(var.section, var.name, "placeholder", "still the example value"))
        else:
            results.append(EnvResult(var.section, var.name, "set"))
    return results
