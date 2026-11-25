import json

import httpx
import respx
from typer.testing import CliRunner

from insidious_cli import main as cli_main
from insidious_cli.rehearse import (GuardianSampler, PassResult, Step, build_report, capacity_gain, merge_step,
                                     plan_steps, render_markdown, run_pass, write_report)

GUARDIAN = "http://guardian.test"
STORE = "http://store.test"
OPSBOT = "http://opsbot.test"


class FakeRunner:
    """Stands in for k6: records calls, returns client-side numbers from a latency curve."""

    def __init__(self, curve=lambda rps: 100.0, err=lambda rps: 0.0):
        self.curve, self.err, self.calls = curve, err, []

    def run(self, target_rps, duration_s, store_url):
        self.calls.append((target_rps, duration_s, store_url))
        return {"checkout_p95_ms": self.curve(target_rps), "checkout_error_rate": self.err(target_rps),
                "rps": target_rps * 0.99, "dropped_iterations": 0}


def guardian_curve(knee: int):
    """Guardian sample handler: p95 grows past 800 ms at `knee` rps."""
    def handler(req):
        body = json.loads(req.content)
        t = body["target_rps"]
        p95 = 300.0 if t < knee else 1500.0
        return httpx.Response(200, json={"p95_ms": {"/checkout": p95, "/": 100.0}, "error_rate": {"/checkout": 0.0},
                                         "rps": t - 1.5, "label": body["label"]})
    return handler


def test_plan_steps():
    assert plan_steps(50, 800, 50) == list(range(50, 801, 50))
    assert plan_steps(50, 800, 50, quick=True) == [50, 300, 550, 800]
    assert plan_steps(50, 120, 50) == [50, 100, 120]


def test_capacity_gain():
    assert capacity_gain(420, 680, 800) == "+62%"
    assert capacity_gain(400, 400, 800) == "+0%"
    assert capacity_gain(400, None, 800) == ">+100%"
    assert capacity_gain(None, None, 800).startswith("n/a")


def test_breaking_point_from_guardian_and_early_stop():
    steps = plan_steps(50, 800, 50)
    with respx.mock as router, httpx.Client() as client:
        router.post(f"{GUARDIAN}/api/rehearse/sample").mock(side_effect=guardian_curve(knee=200))
        runner = FakeRunner()
        res = run_pass("pass1", "heavy_features on", steps, runner, GuardianSampler(GUARDIAN, client), STORE,
                       "/checkout", 800, 0.02, 20)
    assert res.breaking_rps == 200
    assert [s.target_rps for s in res.steps] == [50, 100, 150, 200, 250]  # stopped after 2 breaches in a row
    assert res.stopped_early is True
    assert all(s.source == "guardian" for s in res.steps)
    assert runner.calls[0] == (50, 20, STORE)
    assert res.steps[0].rps == 48.5


def test_isolated_breach_does_not_stop_the_pass():
    seq = {100: 900.0}  # a single blip at 100 rps
    with respx.mock as router, httpx.Client() as client:
        router.post(f"{GUARDIAN}/api/rehearse/sample").mock(side_effect=lambda req: httpx.Response(200, json={
            "p95_ms": {"/checkout": seq.get(json.loads(req.content)["target_rps"], 200.0)},
            "error_rate": {"/checkout": 0.0}, "rps": 1.0}))
        res = run_pass("pass1", "on", [50, 100, 150, 200], FakeRunner(), GuardianSampler(GUARDIAN, client), STORE,
                       "/checkout", 800, 0.02, 5)
    assert res.breaking_rps == 100  # first breaching step, per CONTRACTS.md
    assert len(res.steps) == 4 and res.stopped_early is False


def test_error_rate_breach_counts():
    step = merge_step(300, "/checkout", None, {"p95_ms": {"/checkout": 200.0}, "error_rate": {"/checkout": 0.05},
                                                "rps": 299.0}, 800, 0.02)
    assert step.breached is True


def test_falls_back_to_k6_when_guardian_down():
    with respx.mock as router, httpx.Client() as client:
        router.post(f"{GUARDIAN}/api/rehearse/sample").mock(side_effect=httpx.ConnectError("refused"))
        sampler = GuardianSampler(GUARDIAN, client)
        res = run_pass("pass2", "off", [100, 200, 300, 400], FakeRunner(curve=lambda r: 2000.0 if r >= 300 else 150.0),
                       sampler, STORE, "/checkout", 800, 0.02, 5)
    assert res.breaking_rps == 300 and all(s.source == "k6" for s in res.steps)
    assert sampler.last_error


def test_report_shape_and_files(tmp_path):
    p1 = PassResult("heavy_features on", 420, [Step(400, 398.0, {"/checkout": 700.0}, {"/checkout": 0.0}, False, "guardian"),
                                               Step(420, 419.0, {"/checkout": 1840.0}, {"/checkout": 0.01}, True, "guardian")])
    p2 = PassResult("heavy_features off", 680, [Step(680, 677.0, {"/checkout": 900.0}, {"/checkout": 0.0}, True, "k6")])
    rep = build_report("jeffy", "/checkout", 800, p1, p2, 800)
    assert rep["capacity_gain_if_shed"] == "+62%"
    assert rep["slo"] == {"route": "/checkout", "p95_ms": 800}
    assert rep["pass1"]["breaking_rps"] == 420 and rep["pass2"]["breaking_rps"] == 680
    assert set(rep["pass1"]["steps"][0]) >= {"target_rps", "rps", "p95_ms", "error_rate"}
    assert "pre-scale to 3 replicas" in rep["recommendation"]
    jp, mp = write_report(rep, tmp_path)
    assert jp.name.startswith("readiness-") and jp.suffix == ".json" and mp.suffix == ".md"
    assert json.loads(jp.read_text())["app"] == "jeffy"
    md = render_markdown(rep)
    assert "420 rps" in md and "+62%" in md and "| 420 | 419.0 | 1840 |" in md


# ------------------------------------------------------------- whole command
def _run_cli(monkeypatch, runner_obj, extra_args=()):
    monkeypatch.setattr(cli_main, "K6Runner", lambda script: runner_obj)
    runner_obj.available = lambda: True
    monkeypatch.setenv("GUARDIAN_URL", GUARDIAN)
    monkeypatch.setenv("STORE_URL", STORE)
    monkeypatch.setenv("OPS_BOT_URL", OPSBOT)
    return CliRunner().invoke(cli_main.app, ["rehearse", "--quick", "--cooldown", "0", "--settle", "0",
                                             "--step-seconds", "1", *extra_args])


def _pin_rehearse_config(monkeypatch, tmp_path, **rehearse):
    """Copy the repo's insidious.yaml with a test-owned rehearse profile, so edits to the demo ramp don't break tests."""
    import yaml
    from tests.conftest import REPO
    cfg = yaml.safe_load((REPO / "insidious.yaml").read_text()) or {}
    cfg["rehearse"] = {**(cfg.get("rehearse") or {}), **rehearse}
    path = tmp_path / "insidious.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("INSIDIOUS_CONFIG", str(path))


def test_rehearse_command_end_to_end(monkeypatch, tmp_path):
    _pin_rehearse_config(monkeypatch, tmp_path, start_rps=50, max_rps=800, step_rps=50)
    with respx.mock as router:
        router.get(f"{STORE}/api/flags").mock(return_value=httpx.Response(200, json={"heavy_features": True}))
        dev = router.post(f"{STORE}/api/dev/flags").mock(return_value=httpx.Response(200, json={"ok": True}))
        state = {"heavy": True}

        def sample(req):
            t = json.loads(req.content)["target_rps"]
            knee = 300 if state["heavy"] else 550
            return httpx.Response(200, json={"p95_ms": {"/checkout": 1500.0 if t >= knee else 200.0},
                                             "error_rate": {"/checkout": 0.0}, "rps": float(t)})

        def dev_side(req):
            state["heavy"] = json.loads(req.content)["heavy_features"]
            return httpx.Response(200, json={"ok": True})

        dev.mock(side_effect=dev_side)
        router.post(f"{GUARDIAN}/api/rehearse/sample").mock(side_effect=sample)
        brief = router.post(f"{OPSBOT}/api/brief").mock(return_value=httpx.Response(200, json={"ok": True}))
        res = _run_cli(monkeypatch, FakeRunner())
    assert res.exit_code == 0, res.output
    flips = [json.loads(c.request.content) for c in dev.calls]
    assert flips == [{"heavy_features": True}, {"heavy_features": False}, {"heavy_features": True}]  # restored
    sent = json.loads(brief.calls[0].request.content)
    assert sent["kind"] == "readiness"
    # quick steps 50, 300, 550, 800: pass1 breaks at 300, pass2 at 550 => +83%
    assert sent["report"]["pass1"]["breaking_rps"] == 300 and sent["report"]["pass2"]["breaking_rps"] == 550
    assert sent["report"]["capacity_gain_if_shed"] == "+83%"
    files = sorted((tmp_path / "reports").glob("readiness-*"))
    assert [f.suffix for f in files] == [".json", ".md"]


def test_rehearse_restores_flag_when_k6_fails(monkeypatch):
    from insidious_cli.rehearse import K6Error

    class Boom(FakeRunner):
        def run(self, *a):
            raise K6Error("store unreachable")

    with respx.mock as router:
        router.get(f"{STORE}/api/flags").mock(return_value=httpx.Response(200, json={"heavy_features": True}))
        dev = router.post(f"{STORE}/api/dev/flags").mock(return_value=httpx.Response(200, json={}))
        res = _run_cli(monkeypatch, Boom(), ["--no-brief"])
    assert res.exit_code == 1
    assert json.loads(dev.calls[-1].request.content) == {"heavy_features": True}
