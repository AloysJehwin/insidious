import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from insidious_cli import main as cli_main
from insidious_cli import scan as sc
from insidious_cli.flagsmith_admin import feature_payload

REPO = Path(__file__).resolve().parents[2]
ENV_SECRET = "sk-ant-api03-SHOULDNEVERAPPEAR0123456789abcdef"
AWS_KEY = "AKIAQWERTYUIOPASDFGH"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY"
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"

FIXTURE = {
    ".env": f"ANTHROPIC_API_KEY={ENV_SECRET}\nENABLE_SECRET_THING=1\n",
    ".env.production": "DATABASE_URL=postgres://admin:hunter2pass@db.internal:5432/shop\n",
    "certs/server.pem": f"-----BEGIN PRIVATE KEY-----\n{PEM_BODY}\n-----END PRIVATE KEY-----\n",
    "secrets.json": '{"razorpay": "rzp_live_abcdefghijklmnop"}\n',
    "config/aws.js": (
        f'const AWS_ACCESS_KEY_ID = "{AWS_KEY}";\n'
        f'const awsSecretKey = "{AWS_SECRET}";\n'
        "export const region = 'ap-south-1';\n"
        "export const recos = process.env.ENABLE_RECOMMENDATIONS === '1';\n"
    ),
    "package.json": json.dumps({
        "name": "@acme/shop-web", "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
        "dependencies": {"next": "15.0.0", "react": "19.0.0", "pg": "8.11.0", "sharp": "0.33.0", "openai": "4.0.0",
                         "flagsmith": "4.0.0", "ioredis": "5.0.0"},
    }, indent=2),
    "package-lock.json": '{"lockfileVersion": 3, "ENABLE_FROM_LOCKFILE": true}',
    "app/api/checkout/route.ts": (
        "import { pool } from '@/lib/db';\n"
        "const liveReviews = process.env.FEATURE_LIVE_REVIEWS === 'true';\n"
        "export async function POST(req: Request) {\n"
        "  const r = await pool.query('INSERT INTO orders DEFAULT VALUES RETURNING id');\n"
        "  return Response.json({ id: r.rows[0].id });\n}\n"
    ),
    "app/api/recommendations/route.ts": (
        "export async function GET(req: Request) {\n"
        "  if (process.env.NEXT_PUBLIC_ENABLE_RECOMMENDATIONS !== '1') return Response.json([]);\n"
        "  return Response.json(await computeRecommendations());\n}\n"
    ),
    "app/(shop)/products/[id]/page.tsx": (
        "import { pool } from '@/lib/db';\n"
        "export default async function Page({ params }) {\n"
        "  const p = await pool.query('SELECT * FROM products WHERE id = $1', [params.id]);\n"
        "  return <div>{p.rows[0].name}</div>;\n}\n"
    ),
    "app/page.tsx": "export default function Home() { return <main>Shop</main>; }\n",
    "lib/db.ts": (
        "import { Pool } from 'pg';\n"
        "export const pool = new Pool({\n"
        "  connectionString: process.env.DATABASE_URL,\n"
        "  max: 10,\n"
        "  idleTimeoutMillis: 30000,\n"
        "});\n"
    ),
    "lib/images/resize.ts": "import sharp from 'sharp';\nexport const resize = (b: Buffer) => sharp(b).resize(1200).toBuffer();\n",
    "lib/ai/describe.ts": "import OpenAI from 'openai';\nconst client = new OpenAI();\nexport const describe = async () => client.responses.create({});\n",
    "lib/flags.ts": (
        "import flagsmith from 'flagsmith';\n"
        "export const newCheckout = () => flagsmith.hasFeature('new_checkout');\n"
        "export const features = {\n  wishlist: true,\n  giftWrap: false,\n};\n"
    ),
    "prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n',
    "api/requirements.txt": "fastapi==0.115.0\nsqlalchemy==2.0.0\npsycopg2-binary==2.9.0\nreportlab==4.0\n",
    "api/Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\nCMD [\"uvicorn\", \"main:app\"]\n",
    "api/main.py": (
        "import os\n"
        "from fastapi import FastAPI\n"
        "from sqlalchemy import create_engine\n"
        "app = FastAPI()\n"
        "engine = create_engine(os.environ['DATABASE_URL'], pool_size=5, max_overflow=2)\n"
        "USE_CACHE = os.getenv('USE_CACHE', '1') == '1'\n"
        "PDF_MODE = os.environ.get('PDF_MODE', 'sync')\n"
        "\n"
        "@app.get('/search')\n"
        "def search(q: str):\n    return []\n"
        "\n"
        "@app.post('/reports/export')\n"
        "def export_report():\n    return {}\n"
    ),
    "docker-compose.yml": (
        "services:\n"
        "  web:\n    build: .\n    ports: ['3000:3000']\n"
        "  api:\n    build: ./api\n    deploy:\n      replicas: 2\n"
        "  db:\n    image: postgres:16\n    environment:\n      POSTGRES_PASSWORD: supersecretpw\n"
        "  cache:\n    image: redis:7\n"
    ),
    "node_modules/junk/index.js": "process.env.ENABLE_JUNK_FROM_NODE_MODULES\n",
    ".next/server/chunk.js": "process.env.ENABLE_JUNK_FROM_BUILD\n",
    "tests/checkout.test.ts": "process.env.ENABLE_FROM_TESTS\n",
}


def make_fixture(root: Path) -> Path:
    for rel, content in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (root / "public").mkdir(exist_ok=True)
    (root / "public" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    return root


@pytest.fixture
def fixture_repo(tmp_path):
    return make_fixture(tmp_path / "shop")


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- secret safety
@pytest.mark.parametrize("rel,reason", [
    (".env", "secret files (.env*)"),
    (".env.local", "secret files (.env*)"),
    ("apps/web/.env.production", "secret files (.env*)"),
    ("deploy/tls.key", "keys, certificates and secret files"),
    ("certs/ca.crt", "keys, certificates and secret files"),
    ("certs/readme.txt", "keys, certificates and secret files"),
    ("config/secrets.yaml", "keys, certificates and secret files"),
    ("node_modules/a/b.js", "build output, vendor and cache dirs"),
    (".claude/settings.json", "build output, vendor and cache dirs"),
    ("graphify-out/cache/x.json", "build output, vendor and cache dirs"),
    ("yarn.lock", "lockfiles"),
    ("public/hero.webp", "binaries and media"),
    ("app/api/checkout/route.ts", None),
])
def test_exclusion_reason(rel, reason):
    assert sc.exclusion_reason(rel) == reason


def test_secret_lines_are_redacted():
    lines = [f'const k = "{AWS_KEY}";', "const ok = process.env.API_TOKEN;", "password: 'hunter2hunter2'",
             "-----BEGIN RSA PRIVATE KEY-----", "url = 'postgres://u:p4ss@h/db'", f"key={ENV_SECRET}",
             "export default function Page() {}"]
    out, n = sc.redact_lines(lines)
    assert n == 5
    assert out[1] == lines[1] and out[6] == lines[6]
    assert AWS_KEY not in sc.scrub_text(f"x {AWS_KEY} y") and ENV_SECRET not in sc.scrub_text(ENV_SECRET)


def test_secrets_never_opened_nor_in_payload(fixture_repo, monkeypatch, tmp_path):
    opened: list[str] = []
    real = Path.read_bytes

    def spy(self):
        opened.append(self.as_posix())
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    captured = {}

    class Msgs:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="no tool")])

    client = SimpleNamespace(beta=SimpleNamespace(messages=Msgs()), messages=Msgs())
    outcome = sc.run_scan(fixture_repo, tmp_path / "out", provider="anthropic", client=client)

    assert not any(Path(p).name.startswith(".env") or p.endswith(".pem") or "/certs/" in p
                   or Path(p).name.startswith("secrets") for p in opened), opened
    sent = captured["messages"][0]["content"] + json.dumps(captured["system"])
    for needle in (ENV_SECRET, AWS_KEY, AWS_SECRET, PEM_BODY, "hunter2pass", "supersecretpw", "rzp_live_",
                   "ENABLE_SECRET_THING", "ENABLE_JUNK", "ENABLE_FROM_LOCKFILE", "BEGIN PRIVATE KEY"):
        assert needle not in sent, needle
    # payload still carries the non-secret evidence from the same file
    assert "ENABLE_RECOMMENDATIONS" in sent and "config/aws.js:4" in sent
    ev = outcome.evidence
    assert ev.excluded["secret files (.env*)"] == 2
    assert ev.excluded["keys, certificates and secret files"] == 2
    assert ev.redacted_lines >= 3 and ev.redacted_files >= 2
    notice = ev.exclusion_notice()
    assert "2 secret files (.env*)" in notice and "redacted" in notice
    # Claude never called the tool twice -> heuristic fallback, outputs still written
    assert outcome.analysis.startswith("heuristic (fallback")
    assert all(p.is_file() for p in outcome.paths.values())
    for p in outcome.paths.values():
        text = p.read_text()
        assert AWS_KEY not in text and ENV_SECRET not in text


def test_gitignored_files_are_skipped(fixture_repo):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    (fixture_repo / ".gitignore").write_text("generated/\n")
    (fixture_repo / "generated").mkdir()
    (fixture_repo / "generated" / "flags.js").write_text("process.env.ENABLE_GENERATED_THING\n")
    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)
    ev = sc.Scanner(fixture_repo).run()
    names = {t.name for t in ev.toggles}
    assert "ENABLE_GENERATED_THING" not in names and "ENABLE_RECOMMENDATIONS" in names


# ---------------------------------------------------------------- evidence extraction
def test_evidence_extraction(fixture_repo):
    ev = sc.Scanner(fixture_repo).run()
    toggles = {t.name for t in ev.toggles}
    assert {"ENABLE_RECOMMENDATIONS", "NEXT_PUBLIC_ENABLE_RECOMMENDATIONS", "FEATURE_LIVE_REVIEWS", "USE_CACHE",
            "PDF_MODE"} <= toggles
    assert not toggles & {"ENABLE_SECRET_THING", "ENABLE_JUNK_FROM_NODE_MODULES", "ENABLE_JUNK_FROM_BUILD",
                          "ENABLE_FROM_TESTS", "DATABASE_URL"}
    t = next(t for t in ev.toggles if t.name == "FEATURE_LIVE_REVIEWS")
    assert t.at == "app/api/checkout/route.ts:2" and len(t.snippet.splitlines()) <= 3
    assert "new_checkout" in {f.name for f in ev.flag_sdk}
    assert {"wishlist", "giftWrap"} <= {f.name for f in ev.feature_maps}

    routes = {(r.name, r.kind) for r in ev.routes}
    assert {("/api/checkout", "POST"), ("/api/recommendations", "GET"), ("/products/{id}", "GET"), ("/", "GET"),
            ("/search", "GET"), ("/reports/export", "POST")} <= routes
    rec = next(r for r in ev.routes if r.name == "/api/recommendations")
    assert "recommendations" in rec.extra["cats"]

    heavy = {h.name for h in ev.heavy}
    assert {"hd_images", "ai_features", "pdf_generation"} <= heavy
    assert any(h.name == "hd_images" and h.at == "lib/images/resize.ts:1" for h in ev.heavy)

    engines = {d.name for d in ev.db if d.kind not in ("env", "client")}
    assert {"postgresql", "redis", "sqlalchemy", "prisma"} <= engines
    pool = {(p.name, p.kind) for p in ev.pool}
    assert {("max", "10"), ("pool_size", "5"), ("max_overflow", "2")} <= pool
    assert "DATABASE_URL" in ev.env_names

    svc = {s.root: s for s in ev.services.values()}
    assert svc[""].name == "shop-web" and "nextjs" in svc[""].frameworks
    assert svc["api"].has_dockerfile and "fastapi" in svc["api"].frameworks and svc["api"].ports == ["8000"]
    assert svc["api"].replicas == [2]
    assert {d.name for d in ev.deploy if d.kind == "compose"} == {"web", "api", "db", "cache"}


def test_max_files_prefers_manifests_and_routes(fixture_repo):
    ev = sc.Scanner(fixture_repo, max_files=5).run()
    assert ev.files_read <= 5 and ev.over_max_files > 0
    assert "package.json" in ev.read_files and "api/Dockerfile" in ev.read_files


def test_payload_fits_budget(fixture_repo):
    ev = sc.Scanner(fixture_repo).run()
    big = sc.build_payload(ev, token_budget=600)
    assert len(json.dumps(big)) / sc.CHARS_PER_TOKEN < len(json.dumps(sc.build_payload(ev))) / sc.CHARS_PER_TOKEN


# ---------------------------------------------------------------- mock provider outputs
def test_mock_outputs_match_repo_shapes(fixture_repo, tmp_path):
    out = tmp_path / "scan-out"
    outcome = sc.run_scan(fixture_repo, out, provider="mock")
    assert outcome.analysis.startswith("heuristic (mock")

    cfg = yaml.safe_load((out / "insidious.yaml").read_text())
    repo_cfg = yaml.safe_load((REPO / "insidious.yaml").read_text())
    assert set(repo_cfg) <= set(cfg)
    assert {"database", "agent"} <= set(cfg) and cfg["agent"]["guardrails"]["max_replicas"] == 5
    assert set(cfg["app"]) == set(repo_cfg["app"]) and set(cfg["founder"]) == set(repo_cfg["founder"])
    assert set(cfg["guardian"]) == set(repo_cfg["guardian"]) and set(cfg["rehearse"]) == set(repo_cfg["rehearse"])
    assert cfg["degradable"]["parent_flag"] == "heavy_features"
    assert all({"flag", "fallback"} <= set(f) for f in cfg["degradable"]["features"])
    assert all({"route", "p95_ms", "error_rate"} == set(s) for s in cfg["slos"])
    routes = [s["route"] for s in cfg["slos"]]
    assert routes[0] == "/api/checkout" and "/" in routes
    assert "postgresql" in cfg["database"]["engines"] and cfg["database"]["url_env"] == "DATABASE_URL"

    flags = yaml.safe_load((out / "flagsmith" / "flags.yaml").read_text())
    repo_flags = yaml.safe_load((REPO / "flagsmith" / "flags.yaml").read_text())
    allowed = set().union(*(set(f) for f in repo_flags["features"]))
    names = [f["name"] for f in flags["features"]]
    assert len(names) == len(set(names)) and names[0] == "heavy_features"
    for f in flags["features"]:
        assert {"name", "type", "default_enabled", "description"} <= set(f) <= allowed
        assert f["type"] in ("FLAG", "CONFIG", "MULTIVARIATE")
        assert sc.snake(f["name"]) == f["name"]
        feature_payload(f)  # the Admin API payload builder accepts every entry
    children = {f["name"] for f in flags["features"] if f.get("depends_on") == "heavy_features"}
    assert {"recommendations", "hd_images", "ai_features"} <= children
    assert {"read_from_replica", "db_pool", "ai_agent"} <= set(names)
    rec = next(f for f in flags["features"] if f["name"] == "recommendations")
    assert "app/api/recommendations/route.ts:1" in rec["description"]
    assert set(flags["segments"][0]) == set(repo_flags["segments"][0])

    report = (out / "docs" / "scan-report.md").read_text()
    for heading in ("## Top 10 flags", "## Database", "## Recommended SLOs", "## What Insidious will do on sale day"):
        assert heading in report
    assert "`recommendations`" in report


# ---------------------------------------------------------------- Claude path (mocked client)
VALID = {
    "services": [{"name": "shop-web", "kind": "nextjs", "deploy_hint": "docker-compose", "routes_hot": ["/api/checkout"]}],
    "features": [
        {"flag_name": "Smart Recos", "title": "Smart recommendations", "kind": "degradable",
         "evidence": ["app/api/recommendations/route.ts:1"], "load_impact": "high",
         "fallback_suggestion": "Show best-sellers", "default_enabled": True},
        {"flag_name": "pdf_mode", "title": "PDF mode", "kind": "config", "evidence": ["api/main.py:7"],
         "load_impact": "low", "fallback_suggestion": "Keep sync", "default_enabled": True},
    ],
    "database": {"engines": ["postgresql"], "pool_settings": [{"name": "max", "value": "10", "evidence": "lib/db.ts:4"}],
                 "read_heavy_paths": ["/products/{id}"]},
    "slos": [{"route": "/api/checkout", "p95_ms": 800, "error_rate": 0.02}],
    "scaling": {"min_replicas": 1, "max_replicas": 4, "notes": "pre-scale"},
    "risks": ["No queue for image resizing."],
}


def _tool_msg(data, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, stop_details=None, content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="tool_use", id="toolu_1", name=sc.TOOL_NAME, input=data)])


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                return outer.responses.pop(0)

        self.messages = _M()
        self.beta = SimpleNamespace(messages=_M())


def test_claude_tool_use_drives_outputs(fixture_repo, tmp_path):
    client = FakeClient(_tool_msg(VALID))
    outcome = sc.run_scan(fixture_repo, tmp_path / "o", provider="anthropic", client=client)
    assert outcome.analysis == "claude (anthropic, claude-sonnet-5-5)"
    kw = client.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["tool_choice"] == {"type": "auto"}
    assert kw["tools"][0]["name"] == sc.TOOL_NAME and kw["tools"][0]["strict"] is True
    assert kw["tools"][0]["input_schema"]["additionalProperties"] is False
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert "/api/checkout" in kw["messages"][0]["content"]
    flags = yaml.safe_load(outcome.paths["flags"].read_text())
    by = {f["name"]: f for f in flags["features"]}
    assert by["smart_recos"]["depends_on"] == "heavy_features"  # snake_cased + child of the parent
    assert by["pdf_mode"]["type"] == "CONFIG" and "depends_on" not in by["pdf_mode"]
    cfg = yaml.safe_load(outcome.paths["config"].read_text())
    assert cfg["scaling"]["max_replicas"] == 4 and cfg["database"]["pool"]["pool_size"] == 10


def test_claude_invalid_then_valid_retries_once(fixture_repo, tmp_path):
    bad = dict(VALID, features=[dict(VALID["features"][0], kind="bogus")])
    client = FakeClient(_tool_msg(bad), _tool_msg(VALID))
    outcome = sc.run_scan(fixture_repo, tmp_path / "o", provider="anthropic", client=client)
    assert len(client.calls) == 2 and outcome.analysis.startswith("claude")
    assert "could not be used" in client.calls[1]["messages"][0]["content"]


def test_claude_invalid_twice_falls_back_to_heuristics(fixture_repo, tmp_path):
    bad = dict(VALID, scaling={"min_replicas": 3, "max_replicas": 1, "notes": ""})
    client = FakeClient(_tool_msg(bad), _tool_msg(bad))
    outcome = sc.run_scan(fixture_repo, tmp_path / "o", provider="anthropic", client=client)
    assert len(client.calls) == 2 and outcome.analysis.startswith("heuristic (fallback: invalid output twice")
    assert any(f.flag_name == "recommendations" for f in outcome.result.features)


def test_claude_refusal_falls_back_without_retry(fixture_repo, tmp_path):
    refusal = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"), content=[])
    client = FakeClient(refusal)
    outcome = sc.run_scan(fixture_repo, tmp_path / "o", provider="anthropic", client=client)
    assert len(client.calls) == 1 and "declined" in outcome.analysis


def test_bedrock_uses_plain_messages_with_prefixed_model(fixture_repo, tmp_path):
    client = FakeClient(_tool_msg(VALID))
    outcome = sc.run_scan(fixture_repo, tmp_path / "o", provider="bedrock", client=client)
    kw = client.calls[0]
    assert kw["model"] == "anthropic.claude-sonnet-5-5" and "fallbacks" not in kw and "betas" not in kw
    assert outcome.analysis.startswith("claude (bedrock")


def test_provider_defaults_to_mock_without_key(monkeypatch):
    assert sc.resolve_provider(None) == "mock"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert sc.resolve_provider(None) == "anthropic" and sc.resolve_provider("mock") == "mock"


# ---------------------------------------------------------------- CLI + --apply
def test_cli_scan_apply_prints_plan_without_keys(fixture_repo, tmp_path):
    out = tmp_path / "cli-out"
    with respx.mock(assert_all_called=False) as router:  # no routes: any real request would fail the test
        res = CliRunner().invoke(cli_main.app, ["scan", str(fixture_repo), "--out", str(out), "--apply"],
                                 env={"COLUMNS": "200"})
        assert router.calls.call_count == 0
    assert res.exit_code == 0, res.output
    assert "Planned Flagsmith Admin API calls" in res.output and "printing the plan" in res.output
    assert "heuristic (mock" in res.output and "Excluded (never opened)" in res.output
    assert (out / "flagsmith" / "flags.yaml").is_file()


def test_cli_scan_apply_creates_flags_idempotently(fixture_repo, tmp_path, monkeypatch):
    base, env = "https://fs.test/api/v1", "envkey123"
    monkeypatch.setenv("FLAGSMITH_ADMIN_URL", base)
    monkeypatch.setenv("FLAGSMITH_ORG_API_KEY", "orgkey-real")
    monkeypatch.setenv("FLAGSMITH_ENVIRONMENT_KEY", env)
    monkeypatch.setenv("FLAGSMITH_PROJECT_ID", "42")
    created: list[str] = []
    ids = iter(range(100, 200))

    def create_feature(request):
        body = json.loads(request.content)
        created.append(body["name"])
        return httpx.Response(201, json={"id": next(ids), "name": body["name"]})

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{base}/environments/{env}/").mock(return_value=httpx.Response(200, json={"id": 7}))
        router.get(f"{base}/projects/42/features/").mock(return_value=httpx.Response(200, json={
            "results": [{"id": 1, "name": "heavy_features", "description": "Parent switch."}], "next": None}))
        router.post(f"{base}/projects/42/features/").mock(side_effect=create_feature)
        router.patch(url__regex=rf"{base}/projects/42/features/\d+/").mock(return_value=httpx.Response(200, json={}))
        router.get(url__regex=rf"{base}/environments/{env}/features/\d+/dependencies/").mock(
            return_value=httpx.Response(200, json=[]))
        deps = router.post(url__regex=rf"{base}/environments/{env}/features/\d+/dependencies/\d+/").mock(
            return_value=httpx.Response(201, json={}))
        router.get(f"{base}/projects/42/segments/").mock(return_value=httpx.Response(200, json={"results": [], "next": None}))
        router.post(f"{base}/projects/42/segments/").mock(return_value=httpx.Response(201, json={"id": 9, "name": "high_value_cart"}))
        router.get(f"{base}/features/feature-segments/").mock(return_value=httpx.Response(200, json={"results": []}))
        ov = router.post(f"{base}/environments/{env}/features/1/create-segment-override/").mock(
            return_value=httpx.Response(201, json={}))
        res = CliRunner().invoke(cli_main.app, ["scan", str(fixture_repo), "--out", str(tmp_path / "o"), "--apply",
                                                "--provider", "mock"], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    flags = yaml.safe_load((tmp_path / "o" / "flagsmith" / "flags.yaml").read_text())
    expected = [f["name"] for f in flags["features"] if f["name"] != "heavy_features"]
    assert created == expected  # existing parent not re-created
    n_children = sum(f.get("depends_on") == "heavy_features" for f in flags["features"])
    assert deps.call_count == n_children and ov.call_count == 1
    assert "Planned Flagsmith" not in res.output
