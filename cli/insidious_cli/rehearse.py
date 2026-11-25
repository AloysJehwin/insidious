"""Flag-aware load rehearsal: two passes (heavy_features on/off), breaking point per pass, readiness report."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx


class K6Error(RuntimeError):
    pass


# ---------------------------------------------------------------- step planning
def plan_steps(start: int, max_rps: int, step: int, quick: bool = False, quick_steps: int = 4) -> list[int]:
    if step <= 0 or max_rps < start:
        return [start]
    steps = list(range(start, max_rps + 1, step))
    if steps[-1] != max_rps:
        steps.append(max_rps)
    if quick and len(steps) > quick_steps:
        n = quick_steps
        idx = [round(i * (len(steps) - 1) / (n - 1)) for i in range(n)]
        steps = [steps[i] for i in sorted(set(idx))]
    return steps


def is_breached(p95_ms: float | None, error_rate: float | None, slo_p95_ms: float, slo_error_rate: float) -> bool:
    return (p95_ms is not None and p95_ms > slo_p95_ms) or (error_rate is not None and error_rate > slo_error_rate)


def capacity_gain(b1: int | None, b2: int | None, max_rps: int) -> str:
    """'+62%' from pass1/pass2 breaking points. A pass that never breached gives a lower bound ('>+X%')."""
    if b1 is None:
        return "n/a (pass 1 never breached)"
    if b2 is None:
        pct = round((max_rps - b1) / b1 * 100)
        return f">+{pct}%"
    pct = round((b2 - b1) / b1 * 100)
    return f"{pct:+d}%"


# ---------------------------------------------------------------- runners
class StepRunner(Protocol):
    def run(self, target_rps: int, duration_s: int, store_url: str) -> dict[str, Any]: ...


class K6Runner:
    def __init__(self, script: Path, k6_bin: str | None = None, timeout_pad_s: int = 120):
        self.script = script
        self.k6_bin = k6_bin or os.getenv("K6_BIN") or "k6"
        self.timeout_pad_s = timeout_pad_s

    def available(self) -> bool:
        return shutil.which(self.k6_bin) is not None

    def run(self, target_rps: int, duration_s: int, store_url: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="insidious-k6-") as td:
            summary = Path(td) / "summary.json"
            env = {**os.environ, "TARGET_RPS": str(target_rps), "DURATION": f"{duration_s}s",
                   "STORE_URL": store_url, "SUMMARY_PATH": str(summary)}
            cmd = [self.k6_bin, "run", "--quiet", "--no-color", "--no-usage-report", str(self.script)]
            try:
                proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                      timeout=duration_s + self.timeout_pad_s)
            except FileNotFoundError as e:
                raise K6Error(f"k6 not found ({self.k6_bin}); install k6 or set K6_BIN") from e
            except subprocess.TimeoutExpired as e:
                raise K6Error(f"k6 timed out at {target_rps} rps") from e
            # 99 = thresholds failed (ours always pass, but don't treat it as fatal)
            if proc.returncode not in (0, 99) or not summary.is_file():
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
                raise K6Error(f"k6 exited {proc.returncode} at {target_rps} rps:\n" + "\n".join(tail))
            data = json.loads(summary.read_text())
            data.pop("k6", None)  # keep the step record small
            return data


class GuardianSampler:
    def __init__(self, guardian_url: str, client: httpx.Client):
        self.url = guardian_url.rstrip("/")
        self.client = client
        self.last_error: str | None = None

    def sample(self, label: str, target_rps: int) -> dict[str, Any] | None:
        try:
            r = self.client.post(f"{self.url}/api/rehearse/sample", json={"label": label, "target_rps": target_rps})
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None


# ---------------------------------------------------------------- passes
@dataclass
class Step:
    target_rps: int
    rps: float | None
    p95_ms: dict[str, float | None]
    error_rate: dict[str, float | None]
    breached: bool
    source: str  # guardian | k6
    dropped_iterations: int = 0


@dataclass
class PassResult:
    label: str
    breaking_rps: int | None = None
    steps: list[Step] = field(default_factory=list)
    stopped_early: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "breaking_rps": self.breaking_rps, "stopped_early": self.stopped_early,
                "steps": [asdict(s) for s in self.steps]}


def merge_step(target_rps: int, route: str, k6: dict[str, Any] | None, sample: dict[str, Any] | None,
               slo_p95_ms: float, slo_error_rate: float) -> Step:
    """Prefer guardian's server-side Prometheus numbers; fall back to k6's client-side numbers."""
    g_p95 = ((sample or {}).get("p95_ms") or {}).get(route)
    if sample is not None and g_p95 is not None:
        p95 = dict(sample.get("p95_ms") or {})
        err = dict(sample.get("error_rate") or {})
        rps = sample.get("rps") if sample.get("rps") is not None else (k6 or {}).get("rps")
        source = "guardian"
    else:
        k6 = k6 or {}
        p95 = {route: _round(k6.get("checkout_p95_ms"))}
        err = {route: _round(k6.get("checkout_error_rate"), 4)}
        rps = k6.get("rps")
        source = "k6"
    rps = _round(rps, 1)
    breached = is_breached(p95.get(route), err.get(route), slo_p95_ms, slo_error_rate)
    return Step(target_rps, rps, p95, err, breached, source, int((k6 or {}).get("dropped_iterations") or 0))


def run_pass(label: str, display: str, steps: list[int], runner: StepRunner, sampler: GuardianSampler | None,
             store_url: str, route: str, slo_p95_ms: float, slo_error_rate: float, step_seconds: int,
             on_step: Callable[[str, Step], None] | None = None,
             on_start: Callable[[str, int], None] | None = None,
             stop_after_consecutive: int = 2) -> PassResult:
    res = PassResult(display)
    consecutive = 0
    for target in steps:
        if on_start:
            on_start(label, target)
        k6 = runner.run(target, step_seconds, store_url)
        sample = sampler.sample(label, target) if sampler else None
        step = merge_step(target, route, k6, sample, slo_p95_ms, slo_error_rate)
        res.steps.append(step)
        if on_step:
            on_step(label, step)
        if step.breached:
            consecutive += 1
            if res.breaking_rps is None:
                res.breaking_rps = target
            if consecutive >= stop_after_consecutive:
                res.stopped_early = target != steps[-1]
                break
        else:
            consecutive = 0
    return res


# ---------------------------------------------------------------- report
def build_report(app_name: str, route: str, slo_p95_ms: float, pass1: PassResult, pass2: PassResult, max_rps: int,
                 sale_replicas: int = 3, flag_mode: str = "", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    gain = capacity_gain(pass1.breaking_rps, pass2.breaking_rps, max_rps)
    b1, b2 = pass1.breaking_rps, pass2.breaking_rps
    if b1 is None:
        rec = (f"No breach up to {max_rps} rps with heavy features on. Arm guard anyway; heavy_features stays the "
               f"first lever if traffic exceeds the rehearsal.")
    elif b2 is None or b2 > b1:
        rec = (f"Arm guard with heavy_features as the first lever; pre-scale to {sale_replicas} replicas 30 min "
               f"before the sale.")
    else:
        rec = (f"Shedding heavy_features did not raise the breaking point; pre-scale to {sale_replicas} replicas "
               f"and use scale_out as the first lever.")
    return {
        "app": app_name,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slo": {"route": route, "p95_ms": slo_p95_ms},
        "pass1": pass1.to_json(),
        "pass2": pass2.to_json(),
        "capacity_gain_if_shed": gain,
        "recommendation": rec,
        "flag_control": flag_mode,
    }


def render_markdown(report: dict[str, Any]) -> str:
    slo = report["slo"]
    p1, p2 = report["pass1"], report["pass2"]

    def b(v):
        return f"{v} rps" if v is not None else "no breach"

    lines = [
        f"# Festive readiness report: {report['app']}",
        "",
        f"Generated {report['generated_at']}. SLO: `{slo['route']}` p95 ≤ {slo['p95_ms']:.0f} ms.",
        "",
        f"- **Breaking point, heavy features ON:** {b(p1['breaking_rps'])}",
        f"- **Breaking point, heavy features OFF:** {b(p2['breaking_rps'])}",
        f"- **Capacity gain if shed:** {report['capacity_gain_if_shed']}",
        f"- **Recommendation:** {report['recommendation']}",
        "",
    ]
    for key in ("pass1", "pass2"):
        p = report[key]
        lines += [f"## {key}: {p['label']}", "", "| target rps | actual rps | p95 ms | error rate | breached | source |",
                  "|---:|---:|---:|---:|:---:|:---|"]
        for s in p["steps"]:
            p95 = s["p95_ms"].get(slo["route"])
            err = s["error_rate"].get(slo["route"])
            lines.append(
                f"| {s['target_rps']} | {_fmt(s['rps'], 1)} | {_fmt(p95, 0)} | "
                f"{'n/a' if err is None else f'{err * 100:.2f}%'} | {'yes' if s['breached'] else 'no'} | {s['source']} |")
        lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], out_dir: Path, now: datetime | None = None) -> tuple[Path, Path]:
    now = now or datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"readiness-{now.strftime('%Y%m%d-%H%M')}"
    jp, mp = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    jp.write_text(json.dumps(report, indent=2) + "\n")
    mp.write_text(render_markdown(report) + "\n")
    return jp, mp


def _round(v: Any, nd: int = 1) -> float | None:
    try:
        return None if v is None else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _fmt(v: Any, nd: int) -> str:
    return "n/a" if v is None else f"{float(v):.{nd}f}"
