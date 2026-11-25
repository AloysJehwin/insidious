import json
import subprocess

import httpx
import respx
from typer.testing import CliRunner

from insidious_cli import main as cli_main
from insidious_cli.envcheck import check_env, parse_env_example

EXAMPLE = """\
# ---------- Flagsmith ----------
FLAGSMITH_ORG_API_KEY=xxxxxxxxxxxxxxxx
FLAGSMITH_PROJECT_ID=12345
FLAGSMITH_ADMIN_URL=https://api.flagsmith.com/api/v1
# ---------- NudgeBee ----------
NUDGEBEE_API_TOKEN=sk-nb-xxxxxxxx
NUDGEBEE_WF_PRESALE_ID=
NUDGEBEE_DRY_RUN=0
INSIDIOUS_INTERNAL_TOKEN=change-me-too
"""


def test_env_validation(tmp_path):
    p = tmp_path / ".env.example"
    p.write_text(EXAMPLE)
    assert [v.section for v in parse_env_example(p)][:3] == ["Flagsmith"] * 3
    res = {r.name: r.status for r in check_env(p, {
        "FLAGSMITH_ORG_API_KEY": "real-org-key",
        "FLAGSMITH_PROJECT_ID": "12345",          # still the example id
        "NUDGEBEE_API_TOKEN": "sk-nb-xxxxxxxx",   # untouched placeholder
        "INSIDIOUS_INTERNAL_TOKEN": "",
    })}
    assert res == {
        "FLAGSMITH_ORG_API_KEY": "set",
        "FLAGSMITH_PROJECT_ID": "placeholder",
        "FLAGSMITH_ADMIN_URL": "default",
        "NUDGEBEE_API_TOKEN": "placeholder",
        "NUDGEBEE_WF_PRESALE_ID": "optional",
        "NUDGEBEE_DRY_RUN": "default",
        "INSIDIOUS_INTERNAL_TOKEN": "missing",
    }


def test_init_dry_run_sends_nothing(monkeypatch):
    def no_subprocess(*a, **k):
        raise AssertionError("init --dry-run must not run subprocesses")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    with respx.mock(assert_all_called=False) as router:
        res = CliRunner().invoke(cli_main.app, ["init", "--dry-run"], env={"COLUMNS": "200"})
        assert router.calls.call_count == 0
    assert res.exit_code == 0, res.output
    assert "Planned Flagsmith Admin API calls" in res.output
    assert "create feature heavy_features" in res.output
    assert "accounts_list" in res.output
    assert "NUDGEBEE_WF_INCIDENT_ID not set" in res.output


def test_init_apply_requires_explicit_context(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    res = CliRunner().invoke(cli_main.app, ["init", "--apply"], env={"COLUMNS": "200"})
    assert res.exit_code == 0
    assert "Refusing to apply" in res.output and "never used" in res.output
    assert called == []


def test_init_apply_dry_run_prints_context(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no kubectl")))
    res = CliRunner().invoke(cli_main.app, ["init", "--dry-run", "--apply", "--context", "kind-insidious"],
                             env={"COLUMNS": "200"})
    assert res.exit_code == 0
    assert "kube context: kind-insidious" in res.output
    assert "kubectl --context kind-insidious apply -k" in res.output


def test_guard_dry_run_arms_guardian_only(monkeypatch):
    monkeypatch.setenv("GUARDIAN_URL", "http://guardian.test")
    monkeypatch.setenv("NUDGEBEE_DRY_RUN", "1")
    with respx.mock as router:
        arm = router.post("http://guardian.test/api/arm").mock(return_value=httpx.Response(200, json={"armed": True}))
        feed = router.post("http://guardian.test/api/feed").mock(return_value=httpx.Response(200, json={"ok": True}))
        res = CliRunner().invoke(cli_main.app, ["guard"], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    assert json.loads(arm.calls[0].request.content) == {"armed": True}
    assert "workflow_execute" in res.output and "dry run" in res.output
    assert "dry run" in json.loads(feed.calls[0].request.content)["text"]


def test_status_renders_guardian_state(monkeypatch):
    monkeypatch.setenv("GUARDIAN_URL", "http://guardian.test")
    state = {"armed": True, "slo": {"/checkout": {"p95_ms": 1840.0, "error_rate": 0.0, "target_p95_ms": 800,
                                                  "breaching": True, "breach_count": 3}},
             "rps": 420.5, "replicas": {"store-api": 1}, "flags": {"heavy_features": True},
             "incident": {"id": "slo-checkout-p95-1", "status": "firing", "since": "2026-11-25T14:02:05Z"},
             "last_execution": {"workflow": "sale-incident-guard", "execution_id": "ex-1", "status": "RUNNING",
                                "tasks": [{"id": "t", "type": "core.approval", "status": "WAITING"}]},
             "history": [], "feed": [{"ts": "x", "source": "guardian", "text": "SLO breach"}]}
    with respx.mock as router:
        router.get("http://guardian.test/api/state").mock(return_value=httpx.Response(200, json=state))
        res = CliRunner().invoke(cli_main.app, ["status"], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    for s in ("1840", "slo-checkout-p95-1", "core.approval", "WAITING", "SLO breach"):
        assert s in res.output


def test_reset_dev_endpoint(monkeypatch):
    monkeypatch.setenv("STORE_URL", "http://store.test")
    with respx.mock as router:
        dev = router.post("http://store.test/api/dev/flags").mock(return_value=httpx.Response(200, json={}))
        res = CliRunner().invoke(cli_main.app, ["reset"])
    assert res.exit_code == 0, res.output
    body = json.loads(dev.calls[0].request.content)
    assert body["heavy_features"] is True and body["festive_collection"] is False
    assert body["payment_gateway"] == "razorpay"
