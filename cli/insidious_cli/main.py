"""insidious CLI: init, rehearse, guard, status, reset, scan."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .envcheck import check_env
from .flagctl import (DevFlagController, admin_configured, default_flag_values, make_admin, make_flag_controller,
                      read_store_flags)
from .flagsmith_admin import FlagsmithError
from .nudgebee import TOKEN_CHECK_QUERY, NudgeBeeClient
from .rehearse import GuardianSampler, K6Error, K6Runner, Step, build_report, plan_steps, run_pass, write_report
from . import scan as scanmod
from .settings import (env_flag, env_value, insidious_config, find_repo_root, flags_spec, is_placeholder, load_env,
                       reports_dir, url_env)

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="Insidious: get a self-hosted D2C store through festive sale night.")
console = Console()

OK, WARN, FAIL, SKIP = "[green]✓[/]", "[yellow]![/]", "[red]✗[/]", "[dim]-[/]"
WORKFLOW_ENVS = (("pre-sale-warmup", "NUDGEBEE_WF_PRESALE_ID"), ("sale-incident-guard", "NUDGEBEE_WF_INCIDENT_ID"),
                 ("post-sale-report", "NUDGEBEE_WF_POSTSALE_ID"))


class Ctx:
    def __init__(self) -> None:
        self.root = find_repo_root()
        load_env(self.root)
        self.cfg = insidious_config(self.root)
        self.spec = flags_spec(self.root)

    @property
    def guardian_url(self) -> str:
        return url_env("GUARDIAN_URL", "http://localhost:8002")

    @property
    def store_url(self) -> str:
        return url_env("STORE_URL", "http://localhost:8000")

    @property
    def ops_bot_url(self) -> str:
        return url_env("OPS_BOT_URL", "http://localhost:8003")


def _ctx() -> Ctx:
    return Ctx()


def _nudgebee(dry_run: bool = False) -> NudgeBeeClient:
    return NudgeBeeClient(os.getenv("NUDGEBEE_URL"), os.getenv("NUDGEBEE_API_TOKEN"),
                          os.getenv("NUDGEBEE_ACCOUNT_ID"), dry_run=dry_run)


@app.callback()
def _main(version: bool = typer.Option(False, "--version", help="Show version and exit.")) -> None:
    if version:
        console.print(f"insidious {__version__}")
        raise typer.Exit()


# ===================================================================== init
@app.command()
def init(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the planned Flagsmith/NudgeBee/k8s calls; send nothing."),
    apply: bool = typer.Option(False, "--apply", help="Also kubectl apply -k deploy/k8s (needs --context)."),
    context: Optional[str] = typer.Option(None, "--context", help="kube context for --apply. The default context is never used."),
    show_all_env: bool = typer.Option(False, "--all-env", help="Show every env var, not only problems."),
) -> None:
    """Validate env, create Flagsmith flags + segments, check NudgeBee, optionally apply k8s manifests."""
    c = _ctx()
    checklist: list[tuple[str, str, str]] = []
    console.rule(f"[bold]insidious init[/] {'[yellow](dry run)[/]' if dry_run else ''}")
    console.print(f"repo: {c.root}")

    # 1. config files
    cfg_ok = bool(c.cfg)
    checklist.append((OK if cfg_ok else FAIL, "insidious.yaml", "found" if cfg_ok else "missing or empty"))
    spec_ok = bool(c.spec.get("features"))
    checklist.append((OK if spec_ok else FAIL, "flagsmith/flags.yaml",
                      f"{len(c.spec.get('features') or [])} features, {len(c.spec.get('segments') or [])} segments"
                      if spec_ok else "missing"))

    # 2. env vars
    example = c.root / ".env.example"
    if example.is_file():
        results = check_env(example)
        bad = [r for r in results if not r.ok]
        rows = results if show_all_env else bad
        if rows:
            t = Table(title="Environment (.env)" + ("" if show_all_env else ": needs attention"), show_lines=False)
            for col in ("section", "variable", "status", "note"):
                t.add_column(col)
            colors = {"missing": "red", "placeholder": "yellow", "set": "green", "default": "cyan", "optional": "dim"}
            for r in rows:
                t.add_row(r.section, r.name, f"[{colors.get(r.status, 'white')}]{r.status}[/]", r.note)
            console.print(t)
        checklist.append((OK if not bad else WARN, "environment",
                          f"{len(results) - len(bad)}/{len(results)} ok" + (f", {len(bad)} missing/placeholder" if bad else "")))
    else:
        checklist.append((WARN, "environment", ".env.example not found"))

    # 3. Flagsmith
    env_key = os.getenv("FLAGSMITH_ENVIRONMENT_KEY") or "<FLAGSMITH_ENVIRONMENT_KEY>"
    if dry_run or admin_configured():
        admin = make_admin(dry_run=dry_run)
        try:
            results = admin.apply_spec(c.spec, env_key)
            _print_apply_results(results)
            failed = [r for r in results if r.action in ("failed",)]
            warned = [r for r in results if r.action == "warning"]
            status = OK if not failed and not warned else WARN
            checklist.append((status if not dry_run else WARN, "Flagsmith flags + segments",
                              f"{len(admin.calls)} calls planned (dry run)" if dry_run else
                              f"{sum(r.action == 'created' for r in results)} created, "
                              f"{sum(r.action == 'exists' for r in results)} already present"
                              + (f", {len(warned)} warnings" if warned else "")))
            if dry_run:
                _print_planned_calls(admin)
        except FlagsmithError as e:
            checklist.append((FAIL, "Flagsmith flags + segments", str(e)[:160]))
        finally:
            admin.close()
    else:
        checklist.append((WARN, "Flagsmith flags + segments",
                          "skipped: set FLAGSMITH_ORG_API_KEY, FLAGSMITH_ENVIRONMENT_KEY, FLAGSMITH_PROJECT_ID "
                          "(see the plan with --dry-run)"))

    # 4. NudgeBee token + workflow ids
    nb = _nudgebee(dry_run=dry_run)
    if dry_run:
        console.print(f"[dim]would POST {nb.endpoint}  {TOKEN_CHECK_QUERY}[/]")
        checklist.append((WARN, "NudgeBee token", "not checked (dry run)"))
    elif not nb.has_token:
        checklist.append((WARN, "NudgeBee token", "skipped: NUDGEBEE_URL / NUDGEBEE_API_TOKEN not set"))
    else:
        res = nb.check_token()
        checklist.append((OK if res.ok else FAIL, "NudgeBee token", res.detail))
    nb.close()
    for name, env in WORKFLOW_ENVS:
        val = env_value(env)
        checklist.append((OK if val else WARN, f"NudgeBee workflow {name}", f"{env} set" if val else f"{env} not set"))

    # 5. Kubernetes (optional, explicit context only)
    kdir = c.root / "deploy" / "k8s"
    if not apply:
        checklist.append((SKIP, "Kubernetes manifests", "skipped (use --apply --context <name>)"))
    elif not context:
        console.print(Panel("[red]Refusing to apply:[/] pass [bold]--context <name>[/]. The current default kube "
                            "context is never used.\nContext that would be used: [bold]none[/]", title="kubernetes"))
        checklist.append((FAIL, "Kubernetes manifests", "refused: no --context given"))
    else:
        cmd = ["kubectl", "--context", context, "apply", "-k", str(kdir)]
        console.print(f"kube context: [bold]{context}[/]\n$ {' '.join(cmd)}")
        if dry_run:
            checklist.append((WARN, "Kubernetes manifests", f"would apply with context {context} (dry run)"))
        elif not shutil.which("kubectl"):
            checklist.append((FAIL, "Kubernetes manifests", "kubectl not found"))
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            console.print(proc.stdout.strip() or proc.stderr.strip())
            checklist.append((OK if proc.returncode == 0 else FAIL, "Kubernetes manifests",
                              f"applied with context {context}" if proc.returncode == 0 else f"kubectl exit {proc.returncode}"))

    _print_checklist(checklist)


def _print_apply_results(results) -> None:
    t = Table(title="Flagsmith")
    for col in ("kind", "name", "action", "detail"):
        t.add_column(col)
    colors = {"created": "green", "exists": "cyan", "updated": "green", "planned": "yellow", "warning": "yellow",
              "skipped": "dim", "failed": "red"}
    for r in results:
        t.add_row(r.kind, r.name, f"[{colors.get(r.action, 'white')}]{r.action}[/]", r.detail)
    console.print(t)


def _print_planned_calls(admin) -> None:
    t = Table(title="Planned Flagsmith Admin API calls (not sent)")
    t.add_column("#", justify="right")
    t.add_column("method")
    t.add_column("path", overflow="fold")
    t.add_column("what")
    for i, call in enumerate(admin.calls, 1):
        q = ("?" + "&".join(f"{k}={v}" for k, v in call.params.items())) if call.params else ""
        t.add_row(str(i), call.method, f"{admin.base_url}{call.path}{q}", call.note)
    console.print(t)


def _print_checklist(items: list[tuple[str, str, str]]) -> None:
    t = Table.grid(padding=(0, 2))
    for mark, name, detail in items:
        t.add_row(mark, f"[bold]{name}[/]", detail)
    console.print(Panel(t, title="insidious checklist"))


# ===================================================================== rehearse
@app.command()
def rehearse(
    quick: bool = typer.Option(False, "--quick", help="4 steps per pass instead of the full ramp."),
    store_url: Optional[str] = typer.Option(None, "--store-url", help="Store base URL (default $STORE_URL)."),
    no_brief: bool = typer.Option(False, "--no-brief", help="Don't send the readiness voice brief via ops-bot."),
    guardian_url: Optional[str] = typer.Option(None, "--guardian-url", help="Guardian base URL (default $GUARDIAN_URL)."),
    step_seconds: Optional[int] = typer.Option(None, "--step-seconds", help="Override rehearse.step_seconds."),
    cooldown: Optional[int] = typer.Option(None, "--cooldown", help="Seconds to idle between passes (default: rate window + 5)."),
    settle: Optional[float] = typer.Option(None, "--settle", help="Seconds to wait after a flag flip (default 6 admin / 1 dev)."),
) -> None:
    """Load-test twice (heavy_features on, then off), find each breaking point, write the readiness report."""
    c = _ctx()
    r = c.cfg.get("rehearse") or {}
    slos = c.cfg.get("slos") or [{"route": "/checkout", "p95_ms": 800, "error_rate": 0.02}]
    slo = next((s for s in slos if s.get("route") == "/checkout"), slos[0])
    route, slo_p95, slo_err = slo["route"], float(slo.get("p95_ms", 800)), float(slo.get("error_rate", 0.02))
    start, max_rps, step = int(r.get("start_rps", 50)), int(r.get("max_rps", 800)), int(r.get("step_rps", 50))
    secs = int(step_seconds or r.get("step_seconds", 20))
    window = str((c.cfg.get("guardian") or {}).get("rate_window", "30s")).rstrip("s")
    cool = cooldown if cooldown is not None else (int(window) + 5 if window.isdigit() else 35)
    steps = plan_steps(start, max_rps, step, quick=quick)
    store = (store_url or c.store_url).rstrip("/")
    guardian = (guardian_url or c.guardian_url).rstrip("/")
    parent = (c.cfg.get("degradable") or {}).get("parent_flag", "heavy_features")
    app_name = (c.cfg.get("app") or {}).get("name", "jeffy")
    sale_replicas = int(((c.cfg.get("nudgebee") or {}).get("scale") or {}).get("sale_replicas", 3))

    runner = K6Runner(c.root / "loadtest" / "rehearse.js")
    if not runner.available():
        console.print("[red]k6 not found.[/] Install k6 (https://grafana.com/docs/k6/latest/set-up/install-k6/) or set K6_BIN.")
        raise typer.Exit(2)

    client = httpx.Client(timeout=10)
    flagctl = make_flag_controller(store, client)
    wait = settle if settle is not None else (6.0 if flagctl.mode == "flagsmith-admin" else 1.0)
    before = read_store_flags(store, client) or {}
    original = before.get(parent)
    restore_to = original if isinstance(original, bool) else default_flag_values(c.spec).get(parent, True)
    sampler = GuardianSampler(guardian, client)

    console.rule("[bold]insidious rehearse[/]")
    console.print(f"store {store} · guardian {guardian} · SLO {route} p95 ≤ {slo_p95:.0f} ms, errors ≤ {slo_err:.0%}\n"
                  f"steps {steps} rps × {secs}s · flag control: {flagctl.mode} · {parent} restores to {restore_to}")

    rows: dict[str, list[Step]] = {"pass1": [], "pass2": []}
    status = {"text": "starting"}

    def render():
        t = Table(title="Rehearsal", expand=False)
        for col, j in (("pass", "left"), ("target rps", "right"), ("actual rps", "right"), ("p95 ms", "right"),
                       ("errors", "right"), ("SLO", "center"), ("src", "left")):
            t.add_column(col, justify=j)
        for label, steps_done in rows.items():
            for s in steps_done:
                p95 = s.p95_ms.get(route)
                err = s.error_rate.get(route)
                t.add_row(label, str(s.target_rps), "n/a" if s.rps is None else f"{s.rps:.1f}",
                          "n/a" if p95 is None else f"{p95:.0f}", "n/a" if err is None else f"{err:.1%}",
                          "[red]BREACH[/]" if s.breached else "[green]ok[/]", s.source)
        return Group(t, Text(status["text"], style="dim"))

    p1 = p2 = None
    try:
        with Live(render(), console=console, refresh_per_second=4) as live:
            def on_start(label: str, target: int) -> None:
                status["text"] = f"{label}: running k6 at {target} rps for {secs}s ..."
                live.update(render())

            def on_step(label: str, s: Step) -> None:
                rows[label].append(s)
                status["text"] = f"{label}: {s.target_rps} rps done"
                live.update(render())

            def flip(value: bool) -> None:
                status["text"] = f"setting {parent}={'on' if value else 'off'} via {flagctl.mode}; settling {wait:.0f}s"
                live.update(render())
                flagctl.set_flags({parent: value})
                time.sleep(wait)

            flip(True)
            p1 = run_pass("pass1", f"{parent} on", steps, runner, sampler, store, route, slo_p95, slo_err, secs,
                          on_step, on_start)
            flip(False)
            if cool > 0:
                status["text"] = f"cooling down {cool}s so pass 1 traffic leaves the rate window ..."
                live.update(render())
                time.sleep(cool)
            p2 = run_pass("pass2", f"{parent} off", steps, runner, sampler, store, route, slo_p95, slo_err, secs,
                          on_step, on_start)
            status["text"] = "rehearsal complete"
            live.update(render())
        if not console.is_terminal:
            console.line()
    except K6Error as e:
        console.print(f"[red]k6 failed:[/] {e}")
    except (httpx.HTTPError, RuntimeError, FlagsmithError) as e:
        console.print(f"[red]rehearse aborted:[/] {e}")
    finally:
        try:
            flagctl.set_flags({parent: restore_to})
            console.print(f"{OK} restored {parent}={'on' if restore_to else 'off'}")
        except Exception as e:  # noqa: BLE001
            console.print(f"{FAIL} could not restore {parent}: {e}. Run [bold]insidious reset[/].")

    if p1 is None or p2 is None:
        client.close()
        raise typer.Exit(1)
    if sampler.last_error and all(s.source == "k6" for s in p1.steps + p2.steps):
        console.print(f"{WARN} guardian unavailable ({sampler.last_error}); used k6's client-side numbers")

    report = build_report(app_name, route, slo_p95, p1, p2, max_rps, sale_replicas, flagctl.mode)
    jp, mp = write_report(report, reports_dir(c.root))
    _print_report_summary(report, jp, mp)

    if not no_brief:
        try:
            resp = client.post(f"{c.ops_bot_url}/api/brief", json={"kind": "readiness", "report": report})
            console.print(f"{OK if resp.status_code < 300 else WARN} voice brief → ops-bot (HTTP {resp.status_code})")
        except httpx.HTTPError as e:
            console.print(f"{WARN} could not reach ops-bot for the brief: {e}")
    client.close()


def _print_report_summary(report: dict[str, Any], jp: Path, mp: Path) -> None:
    b1, b2 = report["pass1"]["breaking_rps"], report["pass2"]["breaking_rps"]
    def bp(v):
        return f"[bold]{v} rps[/]" if v is not None else "[bold]no breach[/]"

    txt = (f"Breaking point: {bp(b1)} with heavy features on, {bp(b2)} with them shed → "
           f"capacity gain [bold green]{report['capacity_gain_if_shed']}[/]\n{report['recommendation']}\n\n"
           f"[dim]{jp}\n{mp}[/]")
    console.print(Panel(txt, title="readiness"))


# ===================================================================== guard
@app.command()
def guard(guardian_url: Optional[str] = typer.Option(None, "--guardian-url")) -> None:
    """Sale-day mode: arm guardian and trigger the pre-sale warm-up workflow."""
    c = _ctx()
    g = (guardian_url or c.guardian_url).rstrip("/")
    with httpx.Client(timeout=10) as client:
        try:
            r = client.post(f"{g}/api/arm", json={"armed": True})
            r.raise_for_status()
            console.print(f"{OK} guardian armed ({g}) → {r.json()}")
        except (httpx.HTTPError, ValueError) as e:
            console.print(f"{FAIL} could not arm guardian at {g}: {e}")

        wf = env_value("NUDGEBEE_WF_PRESALE_ID")
        dry = env_flag("NUDGEBEE_DRY_RUN") or not wf or is_placeholder(os.getenv("NUDGEBEE_API_TOKEN")) \
            or is_placeholder(os.getenv("NUDGEBEE_ACCOUNT_ID"))
        nb = _nudgebee()
        inputs = {"source": "insidious guard", "app": (c.cfg.get("app") or {}).get("name", "jeffy")}
        if dry:
            why = "NUDGEBEE_DRY_RUN=1" if env_flag("NUDGEBEE_DRY_RUN") else "workflow id / token / account not set"
            console.print(f"{WARN} dry run ({why}): would POST {nb.endpoint}\n  "
                          f"{nb.workflow_execute_query(wf or '<NUDGEBEE_WF_PRESALE_ID>', inputs)}")
            text = "Guard armed. Pre-sale warm-up workflow: dry run"
        else:
            res = nb.workflow_execute(wf, inputs)
            console.print(f"{OK if res.ok else FAIL} pre-sale-warmup workflow_execute: {res.detail}")
            text = f"Guard armed. Pre-sale warm-up started ({res.detail})" if res.ok else \
                f"Guard armed. Pre-sale warm-up failed: {res.detail}"
        nb.close()
        try:
            client.post(f"{g}/api/feed", json={"source": "guardian", "text": text})
        except httpx.HTTPError:
            pass


# ===================================================================== status
@app.command()
def status(guardian_url: Optional[str] = typer.Option(None, "--guardian-url")) -> None:
    """Show SLOs, flags, replicas, incident and the last NudgeBee execution from guardian."""
    c = _ctx()
    g = (guardian_url or c.guardian_url).rstrip("/")
    try:
        st = httpx.get(f"{g}/api/state", timeout=10).json()
    except (httpx.HTTPError, ValueError) as e:
        console.print(f"{FAIL} guardian not reachable at {g}: {e}")
        raise typer.Exit(1)

    inc = st.get("incident") or {}
    head = Table.grid(padding=(0, 2))
    head.add_row("armed", "[green]yes[/]" if st.get("armed") else "[yellow]no[/]")
    head.add_row("rps", _n(st.get("rps"), 1))
    head.add_row("replicas", ", ".join(f"{k}={'?' if v is None else v}" for k, v in (st.get("replicas") or {}).items()))
    head.add_row("incident", f"{inc.get('id')} [{'red' if inc.get('status') == 'firing' else 'green'}]"
                             f"{inc.get('status')}[/] since {inc.get('since')}" if inc else "none")
    console.print(Panel(head, title=f"guardian {g}"))

    t = Table(title="SLOs")
    for col in ("route", "p95 ms", "target", "errors", "breaching", "count"):
        t.add_column(col)
    for route, s in (st.get("slo") or {}).items():
        t.add_row(route, _n(s.get("p95_ms")), _n(s.get("target_p95_ms")),
                  "n/a" if s.get("error_rate") is None else f"{s['error_rate']:.1%}",
                  "[red]yes[/]" if s.get("breaching") else "[green]no[/]", str(s.get("breach_count", 0)))
    console.print(t)

    flags = st.get("flags") or {}
    if flags:
        ft = Table(title="Flags")
        ft.add_column("flag")
        ft.add_column("value")
        for k, v in flags.items():
            val = "[green]on[/]" if v is True else "[red]off[/]" if v is False else str(v)
            ft.add_row(k, val)
        console.print(ft)

    ex = st.get("last_execution")
    if ex:
        et = Table(title=f"NudgeBee {ex.get('workflow')} · {ex.get('execution_id')} · {ex.get('status')}")
        et.add_column("task")
        et.add_column("type")
        et.add_column("status")
        for task in ex.get("tasks") or []:
            et.add_row(str(task.get("id")), str(task.get("type")), str(task.get("status")))
        console.print(et)
    else:
        console.print("[dim]no NudgeBee execution tracked yet[/]")

    feed = (st.get("feed") or [])[-8:]
    if feed:
        fd = Table(title="Recent feed")
        fd.add_column("ts")
        fd.add_column("source")
        fd.add_column("text")
        for f in feed:
            fd.add_row(str(f.get("ts")), str(f.get("source")), str(f.get("text")))
        console.print(fd)


def _n(v: Any, nd: int = 0) -> str:
    return "n/a" if v is None else f"{float(v):.{nd}f}"


# ===================================================================== reset
@app.command()
def reset(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would change; send nothing."),
    store_url: Optional[str] = typer.Option(None, "--store-url"),
) -> None:
    """Restore every flag to its flagsmith/flags.yaml default."""
    c = _ctx()
    store = (store_url or c.store_url).rstrip("/")
    features = c.spec.get("features") or []
    if dry_run or admin_configured():
        admin = make_admin(dry_run=dry_run)
        env_key = os.getenv("FLAGSMITH_ENVIRONMENT_KEY") or "<FLAGSMITH_ENVIRONMENT_KEY>"
        failures = 0
        for f in features:
            value = f.get("default_value") if f.get("type") in ("CONFIG", "MULTIVARIATE") else None
            kwargs = {"enabled": bool(f.get("default_enabled", False))}
            if value is not None:
                kwargs["value"] = value
            try:
                admin.set_state(env_key, f["name"], **kwargs)
                console.print(f"{OK if not dry_run else WARN} {f['name']} → enabled={kwargs['enabled']}"
                              + (f", value={value!r}" if value is not None else ""))
            except FlagsmithError as e:
                failures += 1
                console.print(f"{FAIL} {f['name']}: {e}")
        if dry_run:
            _print_planned_calls(admin)
        admin.close()
        if failures:
            raise typer.Exit(1)
        return
    values = default_flag_values(c.spec, include_values=False)
    pg = next((f for f in features if f["name"] == "payment_gateway"), None)
    if pg and pg.get("default_value"):
        values["payment_gateway"] = pg["default_value"]
    with httpx.Client(timeout=10) as client:
        try:
            DevFlagController(store, client).set_flags(values)
            console.print(f"{OK} reset via {store}/api/dev/flags → {values}")
        except (httpx.HTTPError, RuntimeError) as e:
            console.print(f"{FAIL} reset failed: {e}")
            raise typer.Exit(1)


# ===================================================================== scan
@app.command()
def scan(
    repo_path: Path = typer.Argument(..., help="Path of the application repository to analyse (read-only)."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output dir (default ./insidious-scan/<repo-name>/)."),
    apply: bool = typer.Option(False, "--apply", help="Create the discovered flags in Flagsmith (idempotent)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --apply: print the planned Flagsmith calls only."),
    provider: Optional[str] = typer.Option(None, "--provider",
                                           help="anthropic | bedrock | mock (default: anthropic if ANTHROPIC_API_KEY is set, else mock)."),
    max_files: int = typer.Option(400, "--max-files", min=1, help="Read at most this many files (most relevant first)."),
    model: str = typer.Option(scanmod.DEFAULT_MODEL, "--model", help="Claude model id."),
) -> None:
    """Analyse an existing app and write insidious.yaml, flagsmith/flags.yaml and docs/scan-report.md for it."""
    repo = repo_path.expanduser().resolve()
    if not repo.is_dir():
        console.print(f"{FAIL} not a directory: {repo}")
        raise typer.Exit(2)
    load_env(find_repo_root())  # Insidious's own .env (provider keys, FLAGSMITH_*); never the scanned repo's
    prov = scanmod.resolve_provider(provider)
    if prov not in ("anthropic", "bedrock", "mock"):
        console.print(f"{FAIL} unknown provider {prov!r} (anthropic | bedrock | mock)")
        raise typer.Exit(2)
    out_dir = (out or Path.cwd() / "insidious-scan" / repo.name).expanduser().resolve()
    console.rule(f"[bold]insidious scan[/] {repo.name}")
    console.print(f"repo {repo}\nout  {out_dir}\nanalysis: {prov}" + (f" ({model})" if prov != "mock" else " (heuristics, no network)"))
    outcome = scanmod.run_scan(repo, out_dir, provider=prov, model=model, max_files=max_files,
                               log=lambda m: console.print(f"{WARN} {m}"))
    ev, res = outcome.evidence, outcome.result
    console.print(f"[dim]{escape(ev.exclusion_notice())}[/]")

    head = Table.grid(padding=(0, 2))
    head.add_row("files read", f"{ev.files_read} of {ev.candidates} candidates")
    head.add_row("evidence", f"{len(ev.toggles)} env toggles · {len(ev.flag_sdk)} flag SDK refs · "
                             f"{len(ev.feature_maps)} config-map keys · {len(ev.heavy)} heavy-work refs · "
                             f"{len(ev.routes)} routes · {len(ev.db) + len(ev.pool)} DB refs")
    head.add_row("payload", f"~{outcome.payload_chars // 4:,} tokens" + ("" if prov != "mock" else " (not sent: mock)"))
    head.add_row("analysis", escape(outcome.analysis))
    head.add_row("services", escape(", ".join(f"{s.name} ({s.kind})" for s in res.services[:8]) or "none"))
    head.add_row("database", escape(", ".join(res.database.engines) or "none found"))
    head.add_row("slos", escape(", ".join(f"{s.route} ≤{int(s.p95_ms)}ms" for s in res.slos)))
    console.print(Panel(head, title="scan summary"))

    t = Table(title=f"Discovered flags ({len(res.features)}; top 15)")
    for col in ("flag", "kind", "load", "parent", "evidence", "when off"):
        t.add_column(col, overflow="fold")
    children = {f.flag_name for f in scanmod._children(res)}
    colors = {"high": "red", "medium": "yellow", "low": "green"}
    for f in res.features[:15]:
        t.add_row(escape(f.flag_name), f.kind, f"[{colors[f.load_impact]}]{f.load_impact}[/]",
                  scanmod.PARENT_FLAG if f.flag_name in children else "", escape(", ".join(f.evidence[:2])),
                  escape(f.fallback_suggestion))
    console.print(t)
    for key, pth in outcome.paths.items():
        console.print(f"{OK} wrote {pth}")

    if apply:
        use_dry = dry_run or not admin_configured()
        if use_dry and not dry_run:
            console.print(f"{WARN} FLAGSMITH_ORG_API_KEY / FLAGSMITH_ENVIRONMENT_KEY / FLAGSMITH_PROJECT_ID not set: "
                          "printing the plan instead (nothing sent)")
        admin = make_admin(dry_run=use_dry)
        env_key = os.getenv("FLAGSMITH_ENVIRONMENT_KEY") or "<FLAGSMITH_ENVIRONMENT_KEY>"
        try:
            results = admin.apply_spec(outcome.flags_spec, env_key)
            _print_apply_results(results)
            if use_dry:
                _print_planned_calls(admin)
        except FlagsmithError as e:
            console.print(f"{FAIL} Flagsmith apply failed: {e}")
            raise typer.Exit(1)
        finally:
            admin.close()


if __name__ == "__main__":  # pragma: no cover
    app()
