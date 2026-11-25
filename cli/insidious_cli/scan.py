"""`insidious scan`: read an existing application, find what can be shed, scaled or tuned on sale night, and write
insidious.yaml + flagsmith/flags.yaml + docs/scan-report.md for it.

Pipeline: list files (git ls-files, .gitignore respected) -> drop secrets/binaries/lockfiles by NAME (never opened)
-> read the most relevant --max-files files, redacting any line that looks like a secret -> static evidence
(toggles, flag SDK calls, heavy routes, database, services) -> Claude (forced-schema tool call, pydantic-validated,
one retry) or the heuristic analyser (mock provider / fallback) -> output files.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

DEFAULT_MODEL = "claude-sonnet-5-5"
TOOL_NAME = "record_scan"
MAX_FILE_BYTES = 200 * 1024
TOKEN_BUDGET = 60_000
CHARS_PER_TOKEN = 3.5
PARENT_FLAG = "heavy_features"

# ===================================================================== file selection
SKIP_DIRS = {
    "node_modules", ".next", "dist", "build", "vendor", ".git", ".claude", "__pycache__", ".venv", "venv", "env",
    ".turbo", ".cache", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", "target", ".nuxt",
    ".output", ".svelte-kit", ".vercel", ".netlify", "storybook-static", "out", "tmp", ".idea", ".vscode", ".expo",
    "Pods", "insidious-scan", "bower_components", ".terraform", ".serverless",
}
SECRET_DIRS = {"certs", "cert", "certificates", "secrets", "secret", ".ssh", ".gnupg", ".aws", "keys"}
SECRET_EXTS = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der", ".jks", ".keystore", ".p8", ".ppk", ".asc",
               ".gpg", ".tfstate", ".tfvars", ".kdbx"}
SECRET_NAMES = {".npmrc", ".pypirc", ".netrc", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                "credentials", "credentials.json", "service-account.json", "serviceaccount.json", ".git-credentials",
                ".dockercfg", "kubeconfig"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "cargo.lock",
             "composer.lock", "gemfile.lock", "bun.lockb", "bun.lock", "uv.lock", "go.sum", "npm-shrinkwrap.json",
             "packages.lock.json", "pdm.lock"}
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".psd",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mp3", ".wav", ".ogg", ".mov", ".webm", ".m4a", ".flac", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".class", ".jar", ".war", ".pyc", ".pyo", ".wasm", ".onnx", ".pt", ".pkl", ".h5", ".sqlite", ".sqlite3", ".db",
    ".parquet", ".avro", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".key", ".dmg", ".iso", ".apk", ".ipa",
    ".node", ".snap", ".lockb",
}
SOURCE_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".go", ".rb", ".php", ".java", ".kt", ".cs",
               ".rs", ".vue", ".svelte", ".astro", ".ex", ".exs", ".scala"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".prisma", ".sql", ".graphql", ".gql",
               ".tf", ".hcl", ".xml", ".properties", ".sh"}
MANIFEST_NAMES = {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml",
                  "build.gradle", "gemfile", "composer.json", "vercel.json", "netlify.toml", "fly.toml", "render.yaml",
                  "procfile", "app.yaml", "serverless.yml", "serverless.yaml", "next.config.js", "next.config.mjs",
                  "next.config.ts", "nuxt.config.ts", "vite.config.ts", "nest-cli.json", "schema.prisma",
                  "drizzle.config.ts", "knexfile.js", "ormconfig.json", "manage.py", "settings.py"}
SERVICE_MARKERS = {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml",
                   "dockerfile", "gemfile", "composer.json"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "integration-tests", "cypress", "playwright",
             "__mocks__", "mocks", "fixtures", "examples", "example", "stories", "storybook", "docs", "doc",
             "benchmarks", "demo", "demos", "_demos", "samples"}

# ===================================================================== secret detection / redaction
# High-confidence token shapes. Used on every line we read AND once more on the final payload (defence in depth).
SECRET_TOKEN_RES = [
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b"),             # AWS access key id
    re.compile(r"(?i)aws.{0,20}?(?:secret|session).{0,20}?['\"=:]\s*['\"]?[A-Za-z0-9/+=]{40}"),  # AWS secret key
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                                                  # OpenAI / Anthropic
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"),                            # Stripe
    re.compile(r"-----BEGIN [A-Z0-9 ]*-----"),                                                # PEM blocks
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),               # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                                            # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),                                                  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),          # JWT
    re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{16,}|\bshp(?:at|ss|ca)_[a-fA-F0-9]{16,}"
               r"|\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),                           # Razorpay/webhooks/Shopify/SendGrid
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@'\"]+:[^\s/@'\"]+@"),                      # URL with user:password@
    re.compile(r"\b[a-fA-F0-9]{32,}\b"),                                                      # long hex
]
LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}")
# key = "literal" assignments of secret-ish names (not env reads, not interpolation)
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|private[_\-]?key|access[_\-]?key|"
    r"client[_\-]?secret|auth[_\-]?key|credential)[\w\-]*['\"]?\s*[:=]\s*"
    r"(?!\s*$)(?![^\n]*(?:process\.env|import\.meta\.env|os\.environ|getenv|Getenv|ENV\[|ENV\.fetch|\$\{|env\(|secretKeyRef|valueFrom))"
    r"['\"]?[^\s'\",;)]{6,}")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
REDACTED = "[redacted: possible secret]"


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _long_random_token(line: str) -> bool:
    for m in LONG_TOKEN_RE.finditer(line):
        tok = m.group(0)
        if (any(c.isdigit() for c in tok) and any(c.isupper() for c in tok) and any(c.islower() for c in tok)
                and tok.count("/") <= 2 and _entropy(tok) >= 4.2):
            return True
    return False


def line_has_secret(line: str) -> bool:
    if any(r.search(line) for r in SECRET_TOKEN_RES):
        return True
    if SECRET_ASSIGN_RE.search(line):
        return True
    return _long_random_token(line)


def redact_lines(lines: list[str]) -> tuple[list[str], int]:
    out, n = [], 0
    for ln in lines:
        if line_has_secret(ln):
            out.append(REDACTED)
            n += 1
        else:
            out.append(ln)
    return out, n


def scrub_text(text: str) -> str:
    """Final payload pass: replace any remaining high-confidence secret token."""
    for r in SECRET_TOKEN_RES:
        text = r.sub("[REDACTED]", text)
    return LONG_TOKEN_RE.sub(lambda m: "[REDACTED]" if _long_random_token(m.group(0)) else m.group(0), text)


def exclusion_reason(rel: str) -> str | None:
    parts = rel.split("/")
    dirs, name = parts[:-1], parts[-1]
    lname = name.lower()
    ext = os.path.splitext(lname)[1]
    ldirs = [d.lower() for d in dirs]
    # secrets first so they are always counted as such
    if lname.startswith(".env") or lname.endswith(".env"):
        return "secret files (.env*)"
    if ext in SECRET_EXTS or any(d in SECRET_DIRS for d in ldirs) or lname in SECRET_NAMES \
            or lname.startswith("secret") or re.match(r"(service[-_]?account|gcp[-_]?key).*\.json$", lname):
        return "keys, certificates and secret files"
    if any(d in SKIP_DIRS for d in dirs) or any(d.endswith("-out") for d in ldirs):
        return "build output, vendor and cache dirs"
    if lname in LOCKFILES:
        return "lockfiles"
    if ext in BINARY_EXTS:
        return "binaries and media"
    if lname.endswith((".min.js", ".min.css", ".map", ".snap", ".log")):
        return "minified, sourcemaps, snapshots, logs"
    return None


def _git_files(repo: Path) -> list[str] | None:
    try:
        inside = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=20)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        proc = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "-co", "--exclude-standard"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # paths are relative to `repo` (git -C), and only cover files under it
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


PRESCAN_PATTERNS = [
    # env toggles
    r"""(process\.env|import\.meta\.env|os\.environ|getenv|Getenv|ENV)[.\[('" ]+(get\(['"])?[A-Z0-9_]*((ENABLE|DISABLE|FEATURE|FLAG|USE)_|_(ENABLED|DISABLED|MODE)([^A-Za-z0-9]|_|$))""",
    # flag SDKs
    r"flagsmith|launchdarkly|unleash|growthbook|posthog|hasFeature\(|isFeatureEnabled\(|useFeatureFlag|getFeatureFlag\(|useFeatureIsOn\(",
    # database pools
    r"new Pool\(|create[A-Za-z]*Pool\(|create_engine\(|pool_size|max_overflow|connectionLimit|connection_limit|"
    r"maxPoolSize|idleTimeoutMillis|connectionTimeoutMillis|new PrismaClient\(",
    # config feature maps
    r"(featureFlags|FEATURE_FLAGS|feature_flags)[[:space:]]*[:=]",
]
PRESCAN_GLOBS = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.py", "*.go", "*.rb", "*.php", "*.vue", "*.svelte"]


def _git_prescan(repo: Path) -> set[str]:
    """File NAMES (never contents) of source files that mention toggles / flag SDKs / DB pools, so --max-files spends
    its budget on them. Source globs only, so .env*, keys and certs are never searched."""
    cmd = ["git", "-C", str(repo), "grep", "-l", "-I", "-E", "--untracked"]
    for pat in PRESCAN_PATTERNS:
        cmd += ["-e", pat]
    cmd += ["--", *PRESCAN_GLOBS, ":(exclude,glob)**/.env*", ":(exclude,glob)**/node_modules/**",
            ":(exclude,glob)**/secret*", ":(exclude,glob)**/certs/**"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    return {ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln}


def _walk_files(repo: Path) -> list[str]:
    out: list[str] = []
    for root, dirnames, filenames in os.walk(repo, followlinks=False):
        # prune heavy build/vendor dirs; secret dirs are still listed (by name only) so they show up in the notice
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_root = os.path.relpath(root, repo)
        for f in filenames:
            out.append(f if rel_root == "." else f"{rel_root}/{f}".replace(os.sep, "/"))
    return out


def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


# ===================================================================== heavy-work categories
@dataclass(frozen=True)
class Category:
    flag: str
    title: str
    words: re.Pattern
    load: str
    fallback: str
    module_words: re.Pattern | None = None  # stricter match for non-route module paths


CATEGORIES = [
    Category("recommendations", "Personalised recommendations",
             re.compile(r"recommend|similar products?|frequently bought|related products?|personali[sz]|upsell|cross sell"),
             "high", "Show cached best-sellers instead of personalised picks"),
    Category("hd_images", "On-the-fly image processing",
             re.compile(r"\bimages?\b|resiz|thumbnail|\bsharp\b|\bjimp\b|watermark|\bcrop\b|\bog image\b|\bimg\b"),
             "high", "Serve pre-sized low-res images",
             re.compile(r"resiz|thumbnail|\bsharp\b|\bjimp\b|watermark|\bog image\b|\bimg proxy\b|"
                        r"image (?:processing|optim\w*|proxy|upload\w*|transform\w*|resiz\w*|pipeline|worker)")),
    Category("pdf_generation", "PDF / invoice generation",
             re.compile(r"\bpdf\b|invoice|puppeteer|pdfkit|jspdf|wkhtmltopdf|weasyprint|reportlab"),
             "high", "Queue the PDF and email it after the rush"),
    Category("ocr", "OCR / document reading", re.compile(r"\bocr\b|tesseract|paddleocr|textract"),
             "high", "Queue documents for later processing"),
    Category("ai_features", "AI / LLM features",
             re.compile(r"\bai\b|\bllm\b|openai|anthropic|claude|\bgpt|gemini|embedding|\bvector|chatbot|copilot|genai"),
             "high", "Hide AI widgets; show static content"),
    Category("search", "Search", re.compile(r"\bsearch|algolia|meilisearch|elastic|typesense|autocomplete|\bfuzzy"),
             "medium", "Simple database search without ranking/suggestions"),
    Category("reports_export", "Reports and exports",
             re.compile(r"\breports?\b|\bexports?\b|\bcsv\b|\bxlsx\b|excel|analytics"),
             "medium", "Queue reports/exports and email them later"),
    Category("background_sync", "Background sync jobs",
             re.compile(r"\bsync\b|synchroni|\bcron\b|reindex|backfill|\bimport jobs?\b"),
             "medium", "Pause sync until after the sale"),
]
CAT_BY_FLAG = {c.flag: c for c in CATEGORIES}

HEAVY_LIB_RE = {
    "hd_images": re.compile(r"""['"](?:sharp|jimp|gm|@napi-rs/canvas|canvas|@vercel/og|next/og|image-size|imagemin)['"]|from PIL\b|import cv2"""),
    "pdf_generation": re.compile(r"""['"](?:puppeteer|puppeteer-core|playwright-core|pdfkit|jspdf|@react-pdf/renderer|pdf-lib|html-pdf|pdfmake)['"]|\breportlab\b|\bweasyprint\b"""),
    "ocr": re.compile(r"""['"](?:tesseract\.js|node-tesseract-ocr)['"]|\bpytesseract\b|\bpaddleocr\b|\btextract\b""", re.I),
    "ai_features": re.compile(r"""['"](?:openai|@anthropic-ai/sdk|@google/generative-ai|@google/genai|ai|@ai-sdk/[\w-]+|langchain|@langchain/[\w-]+|cohere-ai|replicate|@aws-sdk/client-bedrock-runtime)['"]|^\s*(?:import|from)\s+(?:openai|anthropic|langchain)\b"""),
    "search": re.compile(r"""['"](?:algoliasearch|meilisearch|@elastic/elasticsearch|typesense|fuse\.js|flexsearch|minisearch)['"]"""),
    "reports_export": re.compile(r"""['"](?:exceljs|xlsx|json2csv|csv-stringify|papaparse|@json2csv/[\w-]+)['"]|^\s*import\s+(?:pandas|openpyxl)\b"""),
}
IMPORT_LINE_RE = re.compile(r"^\s*(?:import\b|from\s+\S+\s+import\b|export\s+.*\bfrom\b|.*\brequire\(|.*\bimport\()")


def categories_for(text: str, module: bool = False) -> list[str]:
    norm = " ".join(_tokens(text))
    return [c.flag for c in CATEGORIES if ((c.module_words or c.words) if module else c.words).search(norm)]


# ===================================================================== evidence model
@dataclass
class Ev:
    cat: str            # toggle | flag_sdk | feature_map | heavy | route | db | pool | deploy
    file: str
    line: int
    snippet: str = ""
    name: str = ""      # env var / flag key / route path / engine / setting
    kind: str = ""      # env | sdk vendor | map / heavy category / http method / ...
    service: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class ServiceInfo:
    root: str
    name: str
    package_name: str = ""
    frameworks: set = field(default_factory=set)
    deploy_hints: set = field(default_factory=set)
    deps: set = field(default_factory=set)
    scripts: dict = field(default_factory=dict)
    ports: list = field(default_factory=list)
    has_dockerfile: bool = False
    replicas: list = field(default_factory=list)

    @property
    def deployable(self) -> bool:
        return bool(self.has_dockerfile or self.frameworks & WEB_FRAMEWORKS or
                    {"start", "dev", "serve"} & set(self.scripts) or self.deploy_hints or
                    SERVICE_DIR_RE.search(self.root.split("/")[-1] if self.root else ""))


# top-level dirs that are services even without their own manifest (monorepos); bare `app`/`api` are code dirs
SERVICE_DIR_RE = re.compile(r"[-_](?:service|svc|api|app|web|server|backend|frontend|worker|gateway)s?$|"
                            r"^(?:backend|frontend|server|worker|gateway)s?$")
WEB_FRAMEWORKS = {"nextjs", "express", "fastify", "koa", "nestjs", "hono", "remix", "nuxt", "sveltekit", "fastapi",
                  "flask", "django", "react", "vue", "vite", "astro", "expo", "gin", "rails", "laravel"}
FRAMEWORK_DEPS = {"next": "nextjs", "express": "express", "fastify": "fastify", "koa": "koa", "@nestjs/core": "nestjs",
                  "hono": "hono", "@remix-run/node": "remix", "nuxt": "nuxt", "@sveltejs/kit": "sveltekit",
                  "react": "react", "vue": "vue", "vite": "vite", "astro": "astro", "expo": "expo",
                  "react-native": "react-native", "electron": "electron", "fastapi": "fastapi", "flask": "flask",
                  "django": "django", "celery": "celery", "bullmq": "bullmq", "bull": "bull", "agenda": "agenda"}
DB_DEPS = {"pg": "postgresql", "postgres": "postgresql", "@neondatabase/serverless": "postgresql",
           "@vercel/postgres": "postgresql", "psycopg2": "postgresql", "psycopg2-binary": "postgresql",
           "psycopg": "postgresql", "asyncpg": "postgresql", "mysql": "mysql", "mysql2": "mysql", "pymysql": "mysql",
           "mongoose": "mongodb", "mongodb": "mongodb", "pymongo": "mongodb", "motor": "mongodb",
           "redis": "redis", "ioredis": "redis", "@upstash/redis": "redis", "@supabase/supabase-js": "postgresql (supabase)",
           "firebase": "firestore", "firebase-admin": "firestore", "better-sqlite3": "sqlite", "sqlite3": "sqlite",
           "@libsql/client": "libsql", "@planetscale/database": "mysql (planetscale)"}
ORM_DEPS = {"@prisma/client": "prisma", "prisma": "prisma", "drizzle-orm": "drizzle", "knex": "knex",
            "sequelize": "sequelize", "typeorm": "typeorm", "sqlalchemy": "sqlalchemy", "django": "django-orm",
            "kysely": "kysely", "mongoose": "mongoose"}
QUEUE_DEPS = {"bullmq", "bull", "bee-queue", "agenda", "celery", "rq", "@aws-sdk/client-sqs", "amqplib", "kafkajs",
              "inngest", "@trigger.dev/sdk", "pg-boss", "graphile-worker", "@upstash/qstash"}
FLAG_VENDOR_RE = re.compile(r"flagsmith|launchdarkly|ldclient|unleash|growthbook|posthog|@vercel/flags|configcat|"
                            r"splitio|@splitsoftware|optimizely|statsig|flipt|openfeature|flagd", re.I)


@dataclass
class Evidence:
    repo_name: str
    candidates: int = 0
    files_read: int = 0
    over_max_files: int = 0
    excluded: Counter = field(default_factory=Counter)
    redacted_lines: int = 0
    redacted_files: int = 0
    toggles: list[Ev] = field(default_factory=list)
    flag_sdk: list[Ev] = field(default_factory=list)
    feature_maps: list[Ev] = field(default_factory=list)
    heavy: list[Ev] = field(default_factory=list)
    routes: list[Ev] = field(default_factory=list)
    db: list[Ev] = field(default_factory=list)
    pool: list[Ev] = field(default_factory=list)
    deploy: list[Ev] = field(default_factory=list)
    env_names: dict[str, str] = field(default_factory=dict)  # NAME -> first file:line (names only, never values)
    services: dict[str, ServiceInfo] = field(default_factory=dict)
    db_read_files: set = field(default_factory=set)
    read_files: list[str] = field(default_factory=list)

    def exclusion_notice(self) -> str:
        parts = [f"{n} {why}" for why, n in self.excluded.most_common()]
        red = f"; {self.redacted_lines} secret-looking lines redacted in {self.redacted_files} files" \
            if self.redacted_lines else ""
        over = f"; {self.over_max_files} lower-priority files over --max-files" if self.over_max_files else ""
        return "Excluded (never opened): " + (", ".join(parts) or "nothing") + red + over


# ===================================================================== extraction regexes
TOGGLE_NAME_RE = re.compile(r"(?:^|_)(?:ENABLE|DISABLE|FEATURE|FLAG|USE)_|_(?:ENABLED|DISABLED|MODE|FLAG)(?:_|$)")
ENV_READ_RE = re.compile(
    r"""process\.env\.([A-Z][A-Z0-9_]*)|process\.env\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\]|import\.meta\.env\.([A-Z][A-Z0-9_]*)"""
    r"""|os\.environ\[\s*['"]([A-Z][A-Z0-9_]*)['"]|os\.environ\.get\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""
    r"""|\bgetenv\(\s*['"]([A-Z][A-Z0-9_]*)['"]|os\.Getenv\(\s*"([A-Z][A-Z0-9_]*)"|\bENV\[\s*['"]([A-Z][A-Z0-9_]*)['"]"""
    r"""|\bENV\.fetch\(\s*['"]([A-Z][A-Z0-9_]*)['"]|Deno\.env\.get\(\s*['"]([A-Z][A-Z0-9_]*)['"]|\benv\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""
    r"""|\$\{([A-Z][A-Z0-9_]*)(?::?-[^}]*)?\}""")
FLAG_CALL_RE = re.compile(
    r"""\b(hasFeature|getValue|getFeatureValue|isFeatureEnabled|is_feature_enabled|get_feature_value|getFeatureFlag|"""
    r"""getFeatureFlagPayload|useFeatureFlagEnabled|useFeatureFlagVariantKey|useFeatureFlag|useFeatureIsOn|"""
    r"""useFeatureValue|useFeature|isOn|isOff|variation|boolVariation|stringVariation|jsonVariation|isEnabled|"""
    r"""is_enabled|useFlag|isFlagEnabled|feature_enabled|getBooleanValue|getStringValue)\(\s*['"]([A-Za-z0-9_.:\-]+)['"]""")
GENERIC_FLAG_CALLS = {"getValue", "isEnabled", "is_enabled", "isOn", "isOff", "variation", "useFeature", "useFlag",
                      "getFeatureValue", "getBooleanValue", "getStringValue", "feature_enabled", "isFlagEnabled"}
FEATURE_MAP_START_RE = re.compile(r"\b(?:feature[_\-]?flags?|features|toggles|feature[_\-]?toggles)\s*[:=]\s*(?:\{|dict\(|\[)\s*$|"
                                  r"\b(?:FEATURE_FLAGS|FEATURES|FLAGS)\s*(?::\s*[\w<>\[\], ]+)?\s*=\s*\{\s*$", re.I)
FEATURE_MAP_KEY_RE = re.compile(r"""^\s*['"]?([A-Za-z][\w\-]*)['"]?\s*[:=]\s*(true|false|True|False|process\.env|os\.environ|import\.meta\.env)""")
ROUTE_CALL_RE = re.compile(r"""\b(?:app|router|server|api|routes?|r|fastify|hono|apiRouter|v1)\.(get|post|put|patch|delete|all)\(\s*['"`](/[^'"`]*)['"`]""", re.I)
ROUTE_DECOR_RE = re.compile(r"""@\w+\.(get|post|put|patch|delete|route|api_route)\(\s*['"](/[^'"]*)['"]""")
NEST_DECOR_RE = re.compile(r"""@(Get|Post|Put|Patch|Delete)\(\s*['"]([^'"]*)['"]""")
NEXT_METHOD_RE = re.compile(r"""export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b""")
DB_READ_RE = re.compile(r"""\.(?:findMany|findFirst|findUnique|findAll|findOne|find|aggregate|count)\(|\bSELECT\b|\.select\(|\.from\(\s*['"]|\.query\(|\bdb\.(?:select|query)|\bsql`""")
POOL_CTX_RE = re.compile(r"""new\s+Pool\(|\bcreate\w*Pool\(|\bpool\s*[:=]\s*\{|create_engine\(|create_async_engine\(|new\s+Sequelize\(|mongoose\.connect\(|new\s+MongoClient\(|\bpostgres\(|new\s+PrismaClient\(|DataSource\(|createConnection\(|knex\(""")
POOL_KEY_RE = re.compile(r"""\b(max|min|connectionLimit|connection_limit|pool_size|max_overflow|pool_timeout|maxPoolSize|minPoolSize|pool_recycle|idleTimeoutMillis|idle_timeout|connectionTimeoutMillis|connect_timeout|poolSize|max_connections|statement_timeout|acquireTimeoutMillis)['"]?\s*[:=]\s*['"]?(\d+)""")
POOL_KEYS_ANYWHERE = {"connection_limit", "connectionLimit", "pool_size", "max_overflow", "maxPoolSize", "poolSize",
                      "max_connections", "pool_timeout", "idleTimeoutMillis", "connectionTimeoutMillis"}
DB_ENV_RE = re.compile(r"DATABASE|(?:^|_)DB(?:_|$)|POSTGRES|(?:^|_)PG[A-Z]*(?:_|$)|MYSQL|MONGO|REDIS|SUPABASE|NEON|PLANETSCALE|TURSO|SQLITE|PRISMA")
DB_IMAGE_RE = re.compile(r"\b(postgres|postgis|mysql|mariadb|mongo|redis|valkey|pgbouncer|timescale|cockroach|clickhouse|elasticsearch|opensearch|meilisearch)\b")
PRISMA_PROVIDER_RE = re.compile(r"""provider\s*=\s*"(postgresql|postgres|mysql|sqlite|mongodb|sqlserver|cockroachdb)\"""")


def _snip(lines: list[str], i: int, before: int = 0, after: int = 1) -> str:
    lo, hi = max(0, i - before), min(len(lines), i + after + 1)
    out = [ln.strip()[:180] for ln in lines[lo:hi] if ln.strip()]
    return "\n".join(out[:3])


def next_route(rel: str) -> tuple[str, str] | None:
    """File-system routes (Next.js app + pages routers). -> (route, 'api'|'page')."""
    parts = rel.split("/")
    stem, ext = os.path.splitext(parts[-1])
    if ext not in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
        return None
    dirs = parts[:-1]
    if stem in ("page", "route") and "app" in dirs:
        i = len(dirs) - 1 - dirs[::-1].index("app")
        segs = dirs[i + 1:]
        if any(s.startswith("_") for s in segs):
            return None  # private folders are not routable
    elif "pages" in dirs and not stem.startswith("_") and stem not in ("404", "500"):
        i = len(dirs) - 1 - dirs[::-1].index("pages")
        segs = dirs[i + 1:] + ([] if stem == "index" else [stem])
    else:
        return None
    clean = []
    for s in segs:
        if (s.startswith("(") and s.endswith(")")) or s.startswith("@"):
            continue
        m = re.fullmatch(r"\[\[?(?:\.\.\.)?([^\]]+)\]?\]", s)
        clean.append("{" + m.group(1) + "}" if m else s)
    route = "/" + "/".join(clean)
    return route, ("api" if stem == "route" or (clean and clean[0] == "api") else "page")


# ===================================================================== scanner
class Scanner:
    def __init__(self, repo: Path, max_files: int = 400, exclude_dir: Path | None = None):
        self.repo = repo.resolve()
        self.max_files = max_files
        self.exclude_dir = exclude_dir.resolve() if exclude_dir else None
        self.ev = Evidence(repo_name=self.repo.name)
        self.service_roots: list[str] = []
        self.hot: set[str] = set()

    # ---------------------------------------------------------------- listing
    def list_candidates(self) -> list[str]:
        paths = _git_files(self.repo)
        if paths is None:
            paths = _walk_files(self.repo)
        else:
            self.hot = _git_prescan(self.repo)
        excl_rel = None
        if self.exclude_dir:
            try:
                excl_rel = self.exclude_dir.relative_to(self.repo).as_posix() + "/"
            except ValueError:
                excl_rel = None
        keep: list[str] = []
        for rel in paths:
            if excl_rel and rel.startswith(excl_rel):
                continue
            reason = exclusion_reason(rel)
            if reason:
                self.ev.excluded[reason] += 1
                continue
            keep.append(rel)
        roots = set()
        for rel in keep:
            parts = rel.split("/")
            if parts[-1].lower() in SERVICE_MARKERS or parts[-1].lower().startswith("dockerfile"):
                roots.add("/".join(parts[:-1]))
            if len(parts) > 1 and SERVICE_DIR_RE.search(parts[0]):
                roots.add(parts[0])
        self.service_roots = sorted(roots, key=lambda r: (-r.count("/") if r else 1, r))
        return keep

    def service_of(self, rel: str) -> str:
        best = ""
        for root in self.service_roots:
            if root and (rel.startswith(root + "/")) and len(root) > len(best):
                best = root
        return best

    def score(self, rel: str) -> int:
        parts = rel.split("/")
        lname = parts[-1].lower()
        ext = os.path.splitext(lname)[1]
        toks = set(_tokens(rel))
        ldirs = {d.lower() for d in parts[:-1]}
        if lname == "package.json" or lname.startswith("dockerfile") or lname.startswith("docker-compose") \
                or lname.startswith("compose.") or lname in MANIFEST_NAMES or ext == ".prisma":
            s = 100
        elif ext in (".yaml", ".yml") and ldirs & {"k8s", "kubernetes", "manifests", "deploy", "helm", "charts", "kustomize"}:
            s = 90
        elif ext in SOURCE_EXTS:
            s = 20
            nr = next_route(rel)
            if nr:
                s = 85 if nr[1] == "api" else 45
                if "admin" in toks:
                    s -= 15
            elif ldirs & {"routes", "api", "controllers", "handlers", "endpoints", "routers", "views"}:
                s = 70
            elif lname.split(".")[0] in ("server", "app", "main", "index", "worker", "urls") and len(parts) <= 4:
                s = 65
            if toks & {"checkout", "cart", "order", "orders", "payment", "payments", "product", "products", "catalog"}:
                s += 15
            if categories_for(rel):
                s += 25
            if toks & {"db", "database", "prisma", "pool", "redis", "drizzle", "knex", "sequelize", "mongoose", "sql", "repository", "repo"}:
                s += 25
            if toks & {"feature", "features", "flag", "flags", "toggle", "toggles", "config", "settings", "env",
                       "controls", "switches", "constants", "options", "runtime", "experiments"}:
                s += 30
            if rel in self.hot:
                s += 45
        elif ext in CONFIG_EXTS:
            s = 15 if ext != ".sql" else 5
            if toks & {"config", "settings", "features", "flags"}:
                s += 20
        elif ext in (".md", ".txt", ".rst"):
            s = 2
        else:
            s = 1
        if ldirs & TEST_DIRS or re.search(r"\.(test|spec|stories|e2e)\.", lname):
            s -= 60
        return s

    def select(self, candidates: list[str]) -> list[str]:
        scored = sorted(((self.score(r), r) for r in candidates), key=lambda t: (-t[0], t[1].count("/"), t[1]))
        scored = [(s, r) for s, r in scored if s > 0]
        cap = max(30, int(self.max_files * 0.35))
        per_service: Counter = Counter()
        chosen, rest = [], []
        for s, rel in scored:
            svc = self.service_of(rel)
            if len(chosen) < self.max_files and (per_service[svc] < cap or s >= 90):
                chosen.append(rel)
                per_service[svc] += 1
            else:
                rest.append(rel)
        for rel in rest:
            if len(chosen) >= self.max_files:
                break
            chosen.append(rel)
        self.ev.over_max_files = max(0, len(scored) - len(chosen))
        return chosen

    # ---------------------------------------------------------------- reading
    def read(self, rel: str) -> list[str] | None:
        p = self.repo / rel
        try:
            if p.is_symlink() or not p.is_file():
                return None
            if p.stat().st_size > MAX_FILE_BYTES:
                self.ev.excluded["large files (>200 KB)"] += 1
                return None
            raw = p.read_bytes()
        except OSError:
            return None
        if b"\0" in raw[:8192]:
            self.ev.excluded["binaries and media"] += 1
            return None
        text = raw.decode("utf-8", "replace")
        if PRIVATE_KEY_RE.search(text):
            self.ev.excluded["files containing private keys"] += 1
            return None
        lines, n = redact_lines(text.splitlines())
        if n:
            self.ev.redacted_lines += n
            self.ev.redacted_files += 1
        return lines

    def run(self) -> Evidence:
        candidates = self.list_candidates()
        self.ev.candidates = len(candidates)
        for root in self.service_roots:
            self.ev.services[root] = ServiceInfo(root=root, name=(root.split("/")[-1] if root else self.ev.repo_name))
        if "" not in self.ev.services:
            self.ev.services[""] = ServiceInfo(root="", name=self.ev.repo_name)
        for rel in self.select(candidates):
            lines = self.read(rel)
            if lines is None:
                continue
            self.ev.files_read += 1
            self.ev.read_files.append(rel)
            self.extract(rel, lines)
        return self.ev

    # ---------------------------------------------------------------- extraction
    def extract(self, rel: str, lines: list[str]) -> None:
        svc_root = self.service_of(rel)
        svc = self.ev.services.get(svc_root) or self.ev.services[""]
        lname = rel.split("/")[-1].lower()
        ext = os.path.splitext(lname)[1]
        text = "\n".join(lines)

        if lname == "package.json":
            self._package_json(rel, text, svc)
        elif lname.startswith("dockerfile"):
            self._dockerfile(rel, lines, svc)
        elif lname.startswith(("docker-compose", "compose.")) and ext in (".yml", ".yaml"):
            self._compose(rel, text)
        elif ext in (".yml", ".yaml"):
            self._k8s(rel, text)
        elif lname in ("requirements.txt", "pyproject.toml"):
            for i, ln in enumerate(lines):
                m = re.match(r"\s*['\"]?([A-Za-z][\w\-\.]*)", ln)
                if m and ln != REDACTED:
                    self._add_dep(svc, m.group(1).lower(), rel, i + 1)
        elif lname == "vercel.json":
            svc.deploy_hints.add("vercel")
        elif lname in ("netlify.toml", "fly.toml", "render.yaml", "procfile", "app.yaml", "serverless.yml"):
            svc.deploy_hints.add({"netlify.toml": "netlify", "fly.toml": "fly.io", "render.yaml": "render",
                                  "procfile": "heroku-style procfile", "app.yaml": "app engine",
                                  "serverless.yml": "serverless framework"}[lname])
        elif ext == ".prisma":
            m = PRISMA_PROVIDER_RE.search(text)
            if m:
                ln = text[:m.start()].count("\n")
                prov = "postgresql" if m.group(1).startswith("postgres") else m.group(1)
                self.ev.db.append(Ev("db", rel, ln + 1, _snip(lines, ln), prov, "prisma", svc_root))
            self._add_dep(svc, "prisma", rel)

        is_src = ext in SOURCE_EXTS
        has_vendor = bool(FLAG_VENDOR_RE.search(text)) if is_src else False
        nr = next_route(rel) if is_src else None
        file_cats = categories_for(rel, module=True) if is_src else []
        if is_src and DB_READ_RE.search(text):
            self.ev.db_read_files.add(rel)
        if nr:
            route, rkind = nr
            methods = sorted(set(NEXT_METHOD_RE.findall(text))) or (["GET"] if rkind == "page" else ["ANY"])
            self.ev.routes.append(Ev("route", rel, 1, "", route, ",".join(methods), svc_root,
                                     {"cats": categories_for(route + " " + rel), "src": "file"}))

        map_until = -1
        map_indent = 0
        for i, ln in enumerate(lines):
            if ln == REDACTED or len(ln) > 2000:
                continue
            # env reads (names only)
            for m in ENV_READ_RE.finditer(ln):
                name = next(g for g in m.groups() if g)
                self.ev.env_names.setdefault(name, f"{rel}:{i + 1}")
                if TOGGLE_NAME_RE.search(name) and (is_src or ext in (".yml", ".yaml", ".toml", ".json")):
                    self.ev.toggles.append(Ev("toggle", rel, i + 1, _snip(lines, i), name, "env", svc_root))
                if DB_ENV_RE.search(name):
                    self.ev.db.append(Ev("db", rel, i + 1, _snip(lines, i, 0, 0), name, "env", svc_root))
                if is_src and re.search(r"(DB|PG|DATABASE|SQL|MYSQL|MONGO|REDIS)_?POOL|POOL_(SIZE|MAX|MIN)", name):  # e.g. DB_POOL_MAX
                    self.ev.pool.append(Ev("pool", rel, i + 1, _snip(lines, i, 0, 0), name, "env var", svc_root))
            if not is_src:
                if ext == ".prisma" and "connection_limit" in ln:
                    self.ev.pool.append(Ev("pool", rel, i + 1, _snip(lines, i, 0, 0), "connection_limit", "prisma", svc_root))
                continue
            # flag SDK calls
            for m in FLAG_CALL_RE.finditer(ln):
                fn, key = m.group(1), m.group(2)
                if fn in GENERIC_FLAG_CALLS and not has_vendor:
                    continue
                vendor = (FLAG_VENDOR_RE.search(text).group(0).lower() if has_vendor else "unknown")
                self.ev.flag_sdk.append(Ev("flag_sdk", rel, i + 1, _snip(lines, i, 0, 0), key, vendor, svc_root))
            # config feature maps
            if i > map_until and FEATURE_MAP_START_RE.search(ln):
                map_until = min(len(lines) - 1, i + 30)
                map_indent = len(ln) - len(ln.lstrip())
            elif i <= map_until:
                stripped = ln.strip()
                if stripped.startswith(("}", "]", ")")) and len(ln) - len(ln.lstrip()) <= map_indent:
                    map_until = -1
                else:
                    km = FEATURE_MAP_KEY_RE.match(ln)
                    if km:
                        self.ev.feature_maps.append(Ev("feature_map", rel, i + 1, _snip(lines, i, 0, 0), km.group(1), "map", svc_root))
            # explicit routes
            for rx in (ROUTE_CALL_RE, ROUTE_DECOR_RE, NEST_DECOR_RE):
                for m in rx.finditer(ln):
                    route = m.group(2) or "/"
                    self.ev.routes.append(Ev("route", rel, i + 1, _snip(lines, i, 0, 1), route, m.group(1).upper(),
                                             svc_root, {"cats": categories_for(route + " " + rel), "src": "code"}))
            # heavy libraries
            if IMPORT_LINE_RE.match(ln):
                for cat, rx in HEAVY_LIB_RE.items():
                    if rx.search(ln):
                        self.ev.heavy.append(Ev("heavy", rel, i + 1, _snip(lines, i, 0, 0), cat, "library", svc_root))
            # database usage + pool settings
            if POOL_CTX_RE.search(ln):
                self.ev.db.append(Ev("db", rel, i + 1, _snip(lines, i, 0, 1), POOL_CTX_RE.search(ln).group(0).strip("( "), "client", svc_root))
                for j in range(i, min(len(lines), i + 12)):
                    for pm in POOL_KEY_RE.finditer(lines[j]):
                        self.ev.pool.append(Ev("pool", rel, j + 1, _snip(lines, j, 0, 0), pm.group(1), pm.group(2), svc_root))
            else:
                for pm in POOL_KEY_RE.finditer(ln):
                    if pm.group(1) in POOL_KEYS_ANYWHERE:
                        self.ev.pool.append(Ev("pool", rel, i + 1, _snip(lines, i, 0, 0), pm.group(1), pm.group(2), svc_root))

        # heavy modules by path (non-route source files, e.g. lib/recommendations.ts)
        if is_src and file_cats and not nr:
            for cat in file_cats:
                word = CAT_BY_FLAG[cat].module_words or CAT_BY_FLAG[cat].words
                hit = next((k for k, ln in enumerate(lines) if ln != REDACTED and word.search(" ".join(_tokens(ln)))), 0)
                self.ev.heavy.append(Ev("heavy", rel, hit + 1, _snip(lines, hit, 0, 1), cat, "module", svc_root))
        # de-dup pool evidence
        seen = set()
        self.ev.pool = [p for p in self.ev.pool if not ((p.at, p.name) in seen or seen.add((p.at, p.name)))]

    def _add_dep(self, svc: ServiceInfo, dep: str, rel: str, line: int = 1) -> None:
        svc.deps.add(dep)
        if dep in FRAMEWORK_DEPS:
            svc.frameworks.add(FRAMEWORK_DEPS[dep])
        if dep in DB_DEPS:
            self.ev.db.append(Ev("db", rel, line, "", DB_DEPS[dep], f"dependency {dep}", svc.root))
        if dep in ORM_DEPS:
            self.ev.db.append(Ev("db", rel, line, "", ORM_DEPS[dep], f"orm {dep}", svc.root))
        for cat, rx in HEAVY_LIB_RE.items():
            if rx.search(f"'{dep}'"):
                self.ev.heavy.append(Ev("heavy", rel, line, "", cat, f"dependency {dep}", svc.root))
        if FLAG_VENDOR_RE.search(dep):
            self.ev.flag_sdk.append(Ev("flag_sdk", rel, line, "", f"(sdk installed: {dep})", dep, svc.root))

    def _package_json(self, rel: str, text: str, svc: ServiceInfo) -> None:
        try:
            pkg = json.loads(text)
        except ValueError:
            m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
            pkg = {"name": m.group(1)} if m else {}
        if not isinstance(pkg, dict):
            return
        svc.package_name = str(pkg.get("name") or "")
        if svc.package_name:
            svc.name = svc.package_name.split("/")[-1]
        scripts = pkg.get("scripts") or {}
        if isinstance(scripts, dict):
            svc.scripts = {k: str(v)[:120] for k, v in scripts.items() if k in ("dev", "build", "start", "serve", "worker")}
        line_of: dict[str, int] = {}
        for i, ln in enumerate(text.splitlines()):
            m = re.match(r'\s*"([^"]+)"\s*:', ln)
            if m:
                line_of.setdefault(m.group(1), i + 1)
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            deps = pkg.get(key) or {}
            if isinstance(deps, dict):
                for d in deps:
                    self._add_dep(svc, d.lower(), rel, line_of.get(d, 1))
        if pkg.get("workspaces"):
            svc.deploy_hints.add("monorepo workspaces")

    def _dockerfile(self, rel: str, lines: list[str], svc: ServiceInfo) -> None:
        svc.has_dockerfile = True
        svc.deploy_hints.add("docker")
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.upper().startswith(("FROM ", "EXPOSE ", "CMD ", "ENTRYPOINT ")):
                self.ev.deploy.append(Ev("deploy", rel, i + 1, s[:160], s.split()[0].upper(), "dockerfile", svc.root))
            if s.upper().startswith("EXPOSE "):
                svc.ports += [p.split("/")[0] for p in s.split()[1:] if p.split("/")[0].isdigit()]

    def _yaml_docs(self, text: str) -> list[Any]:
        text = "\n".join("#" if ln == REDACTED else ln for ln in text.splitlines())  # keep YAML parseable
        try:
            return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except yaml.YAMLError:
            return []

    def _compose(self, rel: str, text: str) -> None:
        for doc in self._yaml_docs(text):
            for name, spec in (doc.get("services") or {}).items():
                if not isinstance(spec, dict):
                    continue
                image = str(spec.get("image") or "")
                build = spec.get("build")
                ctx = build.get("context") if isinstance(build, dict) else build
                ln = next((k + 1 for k, l in enumerate(text.splitlines()) if re.match(rf"^\s+{re.escape(str(name))}:\s*$", l)), 1)
                replicas = ((spec.get("deploy") or {}).get("replicas") if isinstance(spec.get("deploy"), dict) else None)
                self.ev.deploy.append(Ev("deploy", rel, ln, f"{name}: image={image or '-'} build={ctx or '-'}", str(name),
                                         "compose", "", {"image": image, "build": str(ctx or ""), "replicas": replicas}))
                m = DB_IMAGE_RE.search(image.lower())
                if m:
                    self.ev.db.append(Ev("db", rel, ln, f"{name}: image={image}", m.group(1), "compose image", ""))
                if ctx:
                    root = os.path.normpath(os.path.join(os.path.dirname(rel), str(ctx))).replace(os.sep, "/")
                    root = "" if root == "." else root
                    if root in self.ev.services:
                        self.ev.services[root].deploy_hints.add("docker-compose")
                        if replicas:
                            self.ev.services[root].replicas.append(int(replicas))

    def _k8s(self, rel: str, text: str) -> None:
        lines = text.splitlines()
        for doc in self._yaml_docs(text):
            kind = doc.get("kind")
            if kind not in ("Deployment", "StatefulSet", "HorizontalPodAutoscaler", "CronJob", "Service", "Ingress", "DaemonSet"):
                continue
            meta = doc.get("metadata") or {}
            name = str(meta.get("name") or "?")
            spec = doc.get("spec") or {}
            ln = next((k + 1 for k, l in enumerate(lines) if re.search(rf"name:\s*['\"]?{re.escape(name)}['\"]?\s*$", l)), 1)
            extra: dict[str, Any] = {"namespace": meta.get("namespace")}
            if kind in ("Deployment", "StatefulSet"):
                extra["replicas"] = spec.get("replicas")
                containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
                extra["images"] = [str(c.get("image")) for c in containers if isinstance(c, dict)][:3]
                extra["resources"] = [c.get("resources") for c in containers if isinstance(c, dict) and c.get("resources")][:2]
                for img in extra["images"]:
                    m = DB_IMAGE_RE.search(img.lower())
                    if m:
                        self.ev.db.append(Ev("db", rel, ln, f"{kind} {name}: {img}", m.group(1), "k8s image", ""))
            if kind == "HorizontalPodAutoscaler":
                extra["min"] = spec.get("minReplicas")
                extra["max"] = spec.get("maxReplicas")
            self.ev.deploy.append(Ev("deploy", rel, ln, f"{kind} {name}", name, f"k8s {kind}", "", extra))


# ===================================================================== analysis result (pydantic)
def snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s))
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if s and s[0].isdigit():
        s = "f_" + s
    return s[:60]


class ServiceOut(BaseModel):
    name: str
    kind: str = "service"
    deploy_hint: str = ""
    routes_hot: list[str] = Field(default_factory=list)


class FeatureOut(BaseModel):
    flag_name: str
    title: str
    kind: Literal["kill_switch", "degradable", "config", "experiment"]
    evidence: list[str] = Field(default_factory=list)
    load_impact: Literal["high", "medium", "low"]
    fallback_suggestion: str = ""
    default_enabled: bool = True

    @field_validator("flag_name")
    @classmethod
    def _snake(cls, v: str) -> str:
        out = snake(v)
        if not out:
            raise ValueError("flag_name must contain letters or digits")
        return out


class PoolSettingOut(BaseModel):
    name: str
    value: str
    evidence: str = ""


class DatabaseOut(BaseModel):
    engines: list[str] = Field(default_factory=list)
    pool_settings: list[PoolSettingOut] = Field(default_factory=list)
    read_heavy_paths: list[str] = Field(default_factory=list)


class SLOOut(BaseModel):
    route: str
    p95_ms: float = Field(gt=0, le=60_000)
    error_rate: float = Field(ge=0, le=1)


class ScalingOut(BaseModel):
    min_replicas: int = Field(ge=1, le=50)
    max_replicas: int = Field(ge=1, le=50)
    notes: str = ""

    @model_validator(mode="after")
    def _order(self) -> "ScalingOut":
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        return self


class ScanResult(BaseModel):
    services: list[ServiceOut]
    features: list[FeatureOut]
    database: DatabaseOut
    slos: list[SLOOut]
    scaling: ScalingOut
    risks: list[str] = Field(default_factory=list)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S = {"type": "string"}
_SA = {"type": "array", "items": _S}
TOOL_SCHEMA = _obj({
    "services": {"type": "array", "items": _obj({"name": _S, "kind": _S, "deploy_hint": _S, "routes_hot": _SA})},
    "features": {"type": "array", "items": _obj({
        "flag_name": _S, "title": _S,
        "kind": {"type": "string", "enum": ["kill_switch", "degradable", "config", "experiment"]},
        "evidence": _SA,
        "load_impact": {"type": "string", "enum": ["high", "medium", "low"]},
        "fallback_suggestion": _S, "default_enabled": {"type": "boolean"}})},
    "database": _obj({"engines": _SA,
                      "pool_settings": {"type": "array", "items": _obj({"name": _S, "value": _S, "evidence": _S})},
                      "read_heavy_paths": _SA}),
    "slos": {"type": "array", "items": _obj({"route": _S, "p95_ms": {"type": "number"}, "error_rate": {"type": "number"}})},
    "scaling": _obj({"min_replicas": {"type": "integer"}, "max_replicas": {"type": "integer"}, "notes": _S}),
    "risks": _SA,
})
SCAN_TOOL = {
    "name": TOOL_NAME,
    "description": "Record the Insidious scan of this repository: services, sheddable/degradable features (future "
                   "Flagsmith flags), database findings, SLO suggestions, scaling policy and sale-night risks.",
    "strict": True,
    "input_schema": TOOL_SCHEMA,
}

SYSTEM_PROMPT = """You are Insidious's codebase scanner. Insidious keeps small Indian D2C stores up on festive sale nights: with the founder's approval it sheds heavy features through Flagsmith flags (one parent flag, heavy_features, sheds every degradable child at once), scales replicas, and tunes the database pool or adds a read replica.

You receive static evidence extracted from one repository: file:line references, short snippets and names (secrets were removed before you see anything). Produce the scan by calling the record_scan tool:
- services: deployable units. kind e.g. nextjs, express, fastapi, worker, static-site; deploy_hint e.g. vercel, docker, k8s, docker-compose; routes_hot = routes that take the most sale-day traffic or CPU.
- features: things that can be switched off or degraded under load, plus existing toggles worth moving into Flagsmith. flag_name: short stable snake_case. kind: kill_switch (turn off entirely), degradable (serve a cheaper fallback), config (a value, not on/off), experiment (A/B). evidence: "path:line" strings copied exactly from the input. load_impact: CPU/DB/third-party load it adds on a busy night. fallback_suggestion: what shoppers see when it is off, in plain words. default_enabled: its normal state. Never include heavy_features itself. Prefer 5-25 features, most impactful first.
- database: engines, pool settings found (name, value, evidence), read-heavy paths that could move to a read replica.
- slos: 2-6 routes with p95_ms and error_rate targets (checkout is tight, about 800 ms and 0.02).
- scaling: min_replicas and max_replicas (max at most 5) with short notes.
- risks: short plain sentences about what could break on sale night.

Use only facts present in the evidence; never invent files or line numbers. Call record_scan exactly once and do not answer in prose."""


# ===================================================================== evidence -> payload for Claude
def _ev_dict(e: Ev, with_snippet: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {"at": e.at}
    if e.name:
        d["name"] = e.name
    if e.kind:
        d["kind"] = e.kind
    if with_snippet and e.snippet:
        d["snippet"] = e.snippet
    if e.extra.get("cats"):
        d["heavy"] = e.extra["cats"]
    return d


def _dedupe(items: list[Ev], key=lambda e: (e.name, e.kind)) -> list[Ev]:
    seen, out = set(), []
    for e in items:
        k = key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def build_payload(ev: Evidence, token_budget: int = TOKEN_BUDGET) -> dict[str, Any]:
    """Condensed, per-service evidence. Shrinks itself until it fits the token budget."""
    def assemble(per_list: int, snippets: bool) -> dict[str, Any]:
        services = []
        for root, svc in sorted(ev.services.items(), key=lambda kv: kv[0]):
            def mine(items: list[Ev]) -> list[Ev]:
                return [e for e in items if e.service == root]
            routes = sorted(mine(ev.routes), key=lambda e: (not e.extra.get("cats"), not re.search(r"checkout|cart|order|pay|product", e.name), e.name))
            block = {
                "service_root": root or ".",
                "name": svc.name,
                "package": svc.package_name or None,
                "frameworks": sorted(svc.frameworks),
                "deploy_hints": sorted(svc.deploy_hints),
                "scripts": svc.scripts,
                "ports": svc.ports,
                "deps_of_interest": sorted(d for d in svc.deps if d in FRAMEWORK_DEPS or d in DB_DEPS or d in ORM_DEPS
                                           or d in QUEUE_DEPS or FLAG_VENDOR_RE.search(d)
                                           or any(rx.search(f"'{d}'") for rx in HEAVY_LIB_RE.values())),
                "routes": [_ev_dict(e, snippets and e.extra.get("src") == "code") for e in _dedupe(routes, key=lambda e: (e.name, e.kind))[:per_list * 2]],
                "env_toggles": [_ev_dict(e, snippets) for e in _dedupe(mine(ev.toggles))[:per_list]],
                "flag_sdk_calls": [_ev_dict(e, snippets) for e in _dedupe(mine(ev.flag_sdk))[:per_list]],
                "feature_maps": [_ev_dict(e, snippets) for e in _dedupe(mine(ev.feature_maps))[:per_list]],
                "heavy_work": [_ev_dict(e, snippets) for e in _dedupe(mine(ev.heavy), key=lambda e: (e.name, e.file))[:per_list]],
                "database": [_ev_dict(e, snippets) for e in _dedupe(mine(ev.db), key=lambda e: (e.name, e.kind, e.file))[:per_list]],
                "pool_settings": [_ev_dict(e, snippets) for e in mine(ev.pool)[:per_list]],
            }
            if not any(block[k] for k in ("routes", "env_toggles", "flag_sdk_calls", "feature_maps", "heavy_work",
                                          "database", "pool_settings")) and not svc.frameworks and not svc.has_dockerfile:
                continue
            services.append({k: v for k, v in block.items() if v not in (None, [], {}, "")})
        return {
            "repository": ev.repo_name,
            "stats": {"files_read": ev.files_read, "candidate_files": ev.candidates,
                      "excluded": dict(ev.excluded), "redacted_secret_lines": ev.redacted_lines},
            "services": services,
            "deployment": [_ev_dict(e, False) | {k: v for k, v in e.extra.items() if v not in (None, [], {}, "")}
                           for e in ev.deploy if e.kind != "dockerfile"][:per_list * 2]
                          + [_ev_dict(e) for e in ev.deploy if e.kind == "dockerfile"][:per_list],
            "global_database": [_ev_dict(e, snippets) for e in _dedupe([e for e in ev.db if not e.service])][:per_list],
            "env_var_names": sorted(ev.env_names)[:300],
            "files_with_db_reads": sorted(ev.db_read_files)[:per_list * 2],
        }

    per_list, snippets = 60, True
    payload = assemble(per_list, snippets)
    while len(json.dumps(payload)) / CHARS_PER_TOKEN > token_budget and per_list > 4:
        if snippets and per_list <= 30:
            snippets = False
        else:
            per_list = per_list // 2
        payload = assemble(per_list, snippets)
    return payload


def payload_text(payload: dict[str, Any]) -> str:
    return scrub_text(json.dumps(payload, indent=1, default=str))


# ===================================================================== Claude
class ClaudeScanError(Exception):
    pass


def make_client(provider: str, region: str | None = None):
    import anthropic

    if provider == "bedrock":
        region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
        # Mantle (Messages-API endpoint on Bedrock) when the SDK has it; legacy InvokeModel client otherwise.
        cls = getattr(anthropic, "AnthropicBedrockMantle", None) or anthropic.AnthropicBedrock
        return cls(aws_region=region)
    return anthropic.Anthropic()  # ANTHROPIC_API_KEY (or ant profile) from the environment


def bedrock_model_id(model: str) -> str:
    return model if "anthropic." in model else f"anthropic.{model}"


FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}


def _request(client: Any, provider: str, model: str, user_text: str):
    common = dict(
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
        tools=[SCAN_TOOL],
        # forced tool_choice (any/tool) is rejected by current models; steer from the prompt + strict schema
        tool_choice={"type": "auto"},
    )
    if provider == "anthropic" and model in FALLBACK_MODELS:
        # server-side refusal fallback, routed by refusal category
        return client.beta.messages.create(model=model, betas=["server-side-fallback-2026-07-01"],
                                           fallbacks="default", **common)
    return client.messages.create(model=bedrock_model_id(model) if provider == "bedrock" else model, **common)


def _extract_tool_input(msg: Any) -> dict[str, Any]:
    stop = getattr(msg, "stop_reason", None)
    if stop == "refusal":
        details = getattr(msg, "stop_details", None)
        raise ClaudeScanError(f"Claude declined the request ({getattr(details, 'category', None) or 'refusal'})")
    if stop == "max_tokens":
        raise ClaudeScanError("Claude ran out of output tokens before finishing the scan")
    for block in getattr(msg, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            data = block.input
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                raise ClaudeScanError("record_scan input was not an object")
            return data
    raise ClaudeScanError("Claude did not call record_scan")


def analyze_with_claude(payload: dict[str, Any], provider: str, model: str, client: Any = None,
                        log=lambda s: None) -> ScanResult:
    """Ask Claude for the scan; validate with pydantic; retry once on invalid output. Raises ClaudeScanError."""
    import anthropic

    client = client or make_client(provider)
    base = ("Repository evidence (JSON). Snippets are at most 3 lines; secrets were removed.\n\n"
            f"{payload_text(payload)}\n\nCall the record_scan tool with your analysis.")
    user_text, last_err = base, ""
    for attempt in (1, 2):
        try:
            msg = _request(client, provider, model, user_text)
        except anthropic.BadRequestError as e:
            raise ClaudeScanError(f"bad request: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise ClaudeScanError("authentication failed (check ANTHROPIC_API_KEY / AWS credentials)") from e
        except anthropic.PermissionDeniedError as e:
            raise ClaudeScanError(f"permission denied: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise ClaudeScanError(f"model or endpoint not found ({model})") from e
        except anthropic.RateLimitError as e:
            raise ClaudeScanError("rate limited after SDK retries") from e
        except anthropic.APIStatusError as e:
            raise ClaudeScanError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ClaudeScanError(f"could not reach the API: {e}") from e
        try:
            data = _extract_tool_input(msg)
            return ScanResult.model_validate(data)
        except (ValidationError, ValueError, ClaudeScanError) as e:
            if isinstance(e, ClaudeScanError) and "declined" in str(e):
                raise
            last_err = str(e)[:1500]
            log(f"attempt {attempt}: invalid scan output ({last_err.splitlines()[0][:160]})")
            user_text = (base + "\n\nYour previous answer could not be used: " + last_err
                         + "\nCall record_scan again with an input that matches the schema exactly.")
    raise ClaudeScanError(f"invalid output twice: {last_err.splitlines()[0][:200] if last_err else 'unknown'}")


# ===================================================================== heuristic analyser (mock / fallback)
def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _toggle_flag_name(env: str) -> str:
    n = re.sub(r"^(NEXT_PUBLIC_|VITE_|REACT_APP_|EXPO_PUBLIC_|PUBLIC_|NUXT_PUBLIC_)", "", env)
    n = re.sub(r"^(FEATURE_FLAG_|FEATURE_|FLAG_|ENABLE_|DISABLE_|USE_)", "", n)
    n = re.sub(r"(_ENABLED|_DISABLED|_FLAG|_FEATURE)$", "", n)
    return snake(n) or snake(env)


def heuristic_analysis(ev: Evidence) -> ScanResult:
    feats: dict[str, FeatureOut] = {}

    def add(flag: str, title: str, kind: str, evidence: list[str], load: str, fallback: str, default: bool = True):
        flag = snake(flag)
        if not flag or flag == PARENT_FLAG:
            return
        if flag in feats:
            f = feats[flag]
            f.evidence = list(dict.fromkeys(f.evidence + evidence))[:8]
            order = {"low": 0, "medium": 1, "high": 2}
            if order[load] > order[f.load_impact]:
                f.load_impact = load
            return
        feats[flag] = FeatureOut(flag_name=flag, title=title, kind=kind, evidence=evidence[:8], load_impact=load,
                                 fallback_suggestion=fallback, default_enabled=default)

    # heavy work grouped by category
    by_cat: dict[str, list[str]] = {}
    for e in ev.heavy:
        by_cat.setdefault(e.name, []).append(e.at)
    for r in ev.routes:
        for c in r.extra.get("cats") or []:
            by_cat.setdefault(c, []).append(r.at)
    for cat, ats in by_cat.items():
        c = CAT_BY_FLAG[cat]
        add(c.flag, c.title, "degradable", list(dict.fromkeys(ats)), c.load, c.fallback)

    # env toggles
    for e in _dedupe(ev.toggles, key=lambda e: e.name):
        flag = _toggle_flag_name(e.name)
        cats = categories_for(e.name)
        ats = [t.at for t in ev.toggles if t.name == e.name]
        if cats:
            c = CAT_BY_FLAG[cats[0]]
            add(c.flag if flag in (c.flag, snake(c.title)) else flag, _humanize(flag), "degradable", ats, c.load, c.fallback)
        elif re.search(r"_MODE(_|$)", e.name):
            add(flag + "_mode" if not flag.endswith("mode") else flag, f"{_humanize(flag)} mode", "config", ats, "low",
                "Keep the current mode")
        else:
            add(flag, _humanize(flag), "kill_switch", ats, "low", "Feature hidden; core shopping keeps working",
                default=not re.search(r"BETA|EXPERIMENT|NEW_|_V2", e.name))

    # existing flag SDK calls and config maps
    for e in _dedupe([x for x in ev.flag_sdk if not x.name.startswith("(")], key=lambda e: e.name):
        cats = categories_for(e.name)
        ats = [t.at for t in ev.flag_sdk if t.name == e.name]
        kind = "experiment" if re.search(r"exp|test|variant|\bab\b|_ab_|experiment", e.name, re.I) else (
            "degradable" if cats else "kill_switch")
        add(e.name, _humanize(snake(e.name)), kind, ats, CAT_BY_FLAG[cats[0]].load if cats else "low",
            CAT_BY_FLAG[cats[0]].fallback if cats else "Existing flag; keep its current fallback")
    for e in _dedupe(ev.feature_maps, key=lambda e: e.name):
        cats = categories_for(e.name)
        add(e.name, _humanize(snake(e.name)), "degradable" if cats else "kill_switch",
            [t.at for t in ev.feature_maps if t.name == e.name], CAT_BY_FLAG[cats[0]].load if cats else "low",
            CAT_BY_FLAG[cats[0]].fallback if cats else "Feature hidden; core shopping keeps working")

    impact = {"high": 3, "medium": 2, "low": 1}
    kindw = {"degradable": 4, "kill_switch": 2, "experiment": 1, "config": 0}
    features = sorted(feats.values(), key=lambda f: (-(impact[f.load_impact] * 10 + kindw[f.kind] + min(len(f.evidence), 5)), f.flag_name))[:40]

    # database
    engines: list[str] = []
    for e in ev.db:
        if e.kind in ("env", "client"):
            continue
        name = e.name
        if name and name not in engines and not name.isupper():
            engines.append(name)
    if not any(re.search(r"postgres|mysql|mongo|sqlite|firestore|libsql", x) for x in engines):
        env_db = [e.name for e in ev.db if e.kind == "env"]
        if any("POSTGRES" in n or n.startswith("PG") for n in env_db):
            engines.append("postgresql")
        elif any("DATABASE_URL" in n for n in env_db):
            engines.append("sql database (DATABASE_URL)")
    pool = [PoolSettingOut(name=p.name, value=str(p.kind), evidence=p.at)
            for p in _dedupe(ev.pool, key=lambda e: (e.name, e.kind, e.service))][:12]
    read_paths: list[str] = []
    for r in ev.routes:
        if r.name in read_paths:
            continue
        get_like = any(m in r.kind for m in ("GET", "ANY", "ALL"))
        if re.search(r"cron|webhook|internal|admin|staff|health|ready|auth|login|track", r.name):
            continue  # shopper-facing reads only
        if get_like and (r.file in ev.db_read_files or re.search(r"product|catalog|categor|collection|search|listing|shop|browse", r.name)):
            read_paths.append(r.name)
    database = DatabaseOut(engines=engines[:8], pool_settings=pool, read_heavy_paths=read_paths[:12])

    # services
    services: list[ServiceOut] = []
    for root, svc in sorted(ev.services.items()):
        if not svc.deployable:
            continue
        mine = [r for r in ev.routes if r.service == root]
        if not mine and not svc.has_dockerfile and not svc.frameworks:
            continue
        hot = [r.name for r in sorted(mine, key=lambda r: (not re.search(r"checkout|cart|order|pay", r.name),
                                                           not r.extra.get("cats"), r.name))]
        frameworks = set(svc.frameworks)
        if any(r.extra.get("src") == "file" for r in mine):
            frameworks.add("nextjs")
        hints = set(svc.deploy_hints)
        short = (root.split("/")[-1] if root else svc.name).lower()
        for d in ev.deploy:
            if d.kind in ("k8s Deployment", "k8s StatefulSet") and short and (short in d.name.lower() or d.name.lower() in short):
                hints.add("k8s")
            if d.kind == "compose" and short and short == d.name.lower():
                hints.add("docker-compose")
        kind = next((k for k in ("nextjs", "nestjs", "express", "fastify", "hono", "koa", "fastapi", "django",
                                 "flask", "remix", "nuxt", "sveltekit", "astro", "expo", "vite", "react")
                     if k in frameworks), "worker" if {"bullmq", "celery", "bull"} & frameworks else
                    ("container" if svc.has_dockerfile else "service"))
        hint = ", ".join(sorted(hints - {"monorepo workspaces"})) or ("vercel (likely)" if kind == "nextjs" else "unknown")
        services.append(ServiceOut(name=svc.name, kind=kind, deploy_hint=hint, routes_hot=list(dict.fromkeys(hot))[:6]))
    services = services[:15]

    # SLOs
    paths = list(dict.fromkeys(r.name for r in ev.routes))
    slos: list[SLOOut] = []

    def pick(rx: str) -> str | None:
        cands = [p for p in paths if re.search(rx, p, re.I) and p not in [s.route for s in slos]]
        return sorted(cands, key=lambda p: (not p.startswith("/api"), len(p)))[0] if cands else None

    for rx, p95, err in ((r"checkout", 800, 0.02), (r"/orders?\b|payment|/pay\b", 800, 0.02), (r"/cart\b", 800, 0.02)):
        p = pick(rx)
        if p:
            slos.append(SLOOut(route=p, p95_ms=p95, error_rate=err))
            if len(slos) >= 2:
                break
    if "/" not in [s.route for s in slos]:  # every store has a home page
        slos.append(SLOOut(route="/", p95_ms=1200, error_rate=0.05))
    for rx, p95, err in ((r"product|pdp|/item", 1000, 0.02), (r"search", 1500, 0.05)):
        p = pick(rx)
        if p and len(slos) < 6:
            slos.append(SLOOut(route=p, p95_ms=p95, error_rate=err))

    # scaling
    reps = [int(d.extra["replicas"]) for d in ev.deploy if isinstance(d.extra.get("replicas"), int)]
    hpa_max = [int(d.extra["max"]) for d in ev.deploy if isinstance(d.extra.get("max"), int)]
    min_r = max(1, min(reps)) if reps else 1
    max_r = max(min_r, min(5, max(hpa_max) if hpa_max else 3 if min_r < 3 else min_r))
    serverless = any("vercel" in s.deploy_hint or "netlify" in s.deploy_hint for s in services)
    notes = (f"Found replicas={sorted(set(reps))}" if reps else "No replica counts found") + \
            (f", HPA max={max(hpa_max)}" if hpa_max else "") + \
            ("; serverless hosting scales automatically, so Insidious mainly sheds features and guards the DB." if serverless else
             f"; pre-scale to {min(max_r, 3)} replicas 30 minutes before the sale.")
    scaling = ScalingOut(min_replicas=min_r, max_replicas=max_r, notes=notes)

    # risks
    risks: list[str] = []
    real_sdk = [e for e in ev.flag_sdk if not e.name.startswith("(")]
    if ev.toggles and not real_sdk:
        risks.append("Feature switches are environment variables today, so turning one off needs a redeploy; "
                     "as Flagsmith flags they become a one-tap change.")
    all_deps = set().union(*(s.deps for s in ev.services.values())) if ev.services else set()
    heavy_names = [CAT_BY_FLAG[c].title.lower() for c in by_cat]
    if heavy_names and not (all_deps & QUEUE_DEPS):
        risks.append(f"Heavy work ({', '.join(heavy_names[:4])}) runs inside web requests with no job queue found; "
                     "under sale load it competes with checkout for CPU.")
    if database.engines and not pool:
        risks.append("No database connection-pool limit found; a traffic spike can exhaust database connections.")
    if serverless and database.engines:
        risks.append("Serverless instances each open their own database connections; use a pooler (PgBouncer, "
                     "Supabase/Neon pooler or Prisma Accelerate) before the sale.")
    if "ai_features" in by_cat:
        risks.append("AI/LLM calls add third-party latency and per-request cost; keep them behind a kill switch.")
    if not any(re.search(r"checkout|order|pay|cart", s.route) for s in slos):
        risks.append("No checkout route was found automatically; set the checkout SLO route by hand.")
    if ev.redacted_lines:
        risks.append(f"{ev.redacted_lines} lines looked like hard-coded secrets and were redacted; move any real "
                     "secrets into a secret manager.")
    return ScanResult(services=services, features=features, database=database, slos=slos, scaling=scaling, risks=risks)


# ===================================================================== outputs
OPS_FLAGS = [
    {"name": "ai_agent", "type": "FLAG", "default_enabled": True,
     "description": "Insidious ops flag. AI kill switch: off = the agent only notifies, never plans."},
    {"name": "agent_config", "type": "CONFIG", "default_enabled": True,
     "default_value": '{"provider":"anthropic","model":"claude-sonnet-5-5","autopilot":false,"max_actions":4,"language_hint":"plain, non-technical"}',
     "description": "Insidious ops flag. Remote config JSON for the Claude ops agent."},
]
DB_FLAGS = [
    {"name": "read_from_replica", "type": "FLAG", "default_enabled": False,
     "description": "Insidious ops flag. Send catalog/product reads to the read replica (db_read_url)."},
    {"name": "db_read_url", "type": "CONFIG", "default_enabled": True, "default_value": "",
     "description": "Insidious ops flag. Read replica connection URL; set by the agent's RDS watcher, never by hand in git."},
    {"name": "db_pool", "type": "CONFIG", "default_enabled": True,
     "default_value": '{"pool_size":5,"max_overflow":0,"timeout_s":2}',
     "description": "Insidious ops flag. Live database pool size (JSON)."},
]


def _children(result: ScanResult) -> list[FeatureOut]:
    return [f for f in result.features if f.kind == "degradable" and f.load_impact in ("high", "medium")]


def build_flags_spec(result: ScanResult) -> dict[str, Any]:
    reserved = {PARENT_FLAG} | {f["name"] for f in OPS_FLAGS + DB_FLAGS}
    children = {f.flag_name for f in _children(result)}
    features: list[dict[str, Any]] = [{
        "name": PARENT_FLAG, "type": "FLAG", "default_enabled": True,
        "description": "Parent switch. Off = shed every heavy feature at once (" +
                       (", ".join(sorted(children)) if children else "no heavy features found yet") + ").",
    }]
    used = set(reserved)
    for f in result.features:
        name = f.flag_name if f.flag_name not in used else f"{f.flag_name}_feature"
        if name in used:
            continue
        used.add(name)
        ev = ", ".join(f.evidence[:3]) + (f" (+{len(f.evidence) - 3} more)" if len(f.evidence) > 3 else "")
        desc = f"{f.title} [{f.kind}, {f.load_impact} load]." + (f" Evidence: {ev}." if ev else "") + \
               (f" Off = {f.fallback_suggestion}." if f.fallback_suggestion else "")
        spec: dict[str, Any] = {"name": name, "type": "CONFIG" if f.kind == "config" else "FLAG",
                                "default_enabled": bool(f.default_enabled)}
        if f.kind == "config":
            spec["default_value"] = ""
        if f.flag_name in children:
            spec["depends_on"] = PARENT_FLAG
        spec["description"] = desc[:400]
        features.append(spec)
    features += [dict(x) for x in OPS_FLAGS]
    if result.database.engines:
        features += [dict(x) for x in DB_FLAGS]
    segments = [{
        "name": "high_value_cart",
        "description": "Big carts keep the full experience while everyone else is shed (send trait cart_value).",
        "rules": [{"property": "cart_value", "operator": "GREATER_THAN", "value": "2000"}],
        "overrides": [{"feature": PARENT_FLAG, "enabled": True}],
    }]
    return {"features": features, "segments": segments}


def build_insidious_config(result: ScanResult, ev: Evidence) -> dict[str, Any]:
    app_name = snake(ev.repo_name).replace("_", "-") or "app"
    main = next((s for rx in (r"checkout", r"cart", r"order|pay") for s in result.services
                 if any(re.search(rx, r) for r in s.routes_hot)), result.services[0] if result.services else None)
    deployment = snake(main.name).replace("_", "-") if main else app_name
    svc_info = next((s for s in ev.services.values() if main and s.name == main.name), None)
    port = (svc_info.ports[0] if svc_info and svc_info.ports else
            "8000" if main and main.kind in ("fastapi", "django", "flask") else "3000")
    sale = min(result.scaling.max_replicas, max(result.scaling.min_replicas, 3))
    pool_size = next((int(p.value) for p in result.database.pool_settings
                      if p.name in ("pool_size", "max", "connectionLimit", "connection_limit", "maxPoolSize", "poolSize")
                      and str(p.value).isdigit()), 5)
    db_env = next((n for n in ev.env_names if n == "DATABASE_URL"), None) or \
        next((n for n in sorted(ev.env_names) if DB_ENV_RE.search(n) and n.endswith("URL")), None)
    return {
        "app": {"name": app_name, "namespace": "store", "deployment": deployment, "url": f"http://{deployment}:{port}"},
        "founder": {"name": "<founder name>", "phone_env": "FOUNDER_PHONE", "language": "hi-IN"},
        "services": [s.model_dump() for s in result.services],
        "slos": [{"route": s.route, "p95_ms": int(s.p95_ms), "error_rate": s.error_rate} for s in result.slos],
        "guardian": {"interval_seconds": 5, "breach_samples": 3, "recover_samples": 3, "rate_window": "30s"},
        "degradable": {"parent_flag": PARENT_FLAG,
                       "features": [{"flag": f.flag_name, "fallback": snake(f.fallback_suggestion)[:40] or "off",
                                     "load_impact": f.load_impact} for f in _children(result)]},
        "rehearse": {"start_rps": 40, "max_rps": 480, "step_rps": 40, "step_seconds": 20},
        "nudgebee": {"rule_name": "insidious-slo",
                     "workflows": {"presale_env": "NUDGEBEE_WF_PRESALE_ID", "incident_env": "NUDGEBEE_WF_INCIDENT_ID",
                                   "postsale_env": "NUDGEBEE_WF_POSTSALE_ID"},
                     "scale": {"normal_replicas": result.scaling.min_replicas, "sale_replicas": sale}},
        "scaling": result.scaling.model_dump(),
        "database": {
            "engines": result.database.engines,
            "url_env": db_env,
            "pool": {"pool_size": pool_size, "max_overflow": 0, "timeout_s": 2},
            "pool_settings_found": [p.model_dump() for p in result.database.pool_settings],
            "read_heavy_paths": result.database.read_heavy_paths,
            "replica_flag": "read_from_replica", "read_url_flag": "db_read_url",
            "slo": {"pool_utilisation": 0.9, "wait_p95_s": 0.25},
        },
        "agent": {"guardrails": {"max_replicas": 5, "allowed_namespaces": ["store"],
                                 "allowed_db_instance_classes": ["db.t4g.micro", "db.t4g.small", "db.t4g.medium"],
                                 "max_memory_limit": "2Gi", "max_cpu_limit": "2", "rds_id_prefix": ["insidious-", "festivekit-"]}},
    }


def _md_cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def build_report(result: ScanResult, ev: Evidence, meta: dict[str, Any], flags_spec: dict[str, Any]) -> str:
    children = _children(result)
    impact = {"high": 3, "medium": 2, "low": 1}
    top = sorted(result.features, key=lambda f: (-impact[f.load_impact], f.kind != "degradable"))[:10]
    L: list[str] = []
    L.append(f"# Insidious scan: {ev.repo_name}\n")
    L.append(f"Generated {meta['generated_at']} by `insidious scan` · analysis: **{meta['analysis']}** · "
             f"{ev.files_read} files read of {ev.candidates} candidates.\n")
    L.append("## In plain words\n")
    heavy = ", ".join(f.title.lower() for f in children[:5]) or "no obviously heavy features"
    L.append(f"We looked through the code of **{ev.repo_name}** and found {len(result.services)} deployable "
             f"service(s), {len(result.features)} features that can become switches, and "
             f"{'a ' + ' + '.join(result.database.engines[:3]) + ' database' if result.database.engines else 'no database'}. "
             f"On sale night Insidious can switch off {heavy} with one tap (flag `{PARENT_FLAG}`) so checkout stays fast.\n")
    L.append("## Services\n")
    L.append("| service | kind | deploy | hot routes |\n|---|---|---|---|")
    for s in result.services:
        L.append(f"| {_md_cell(s.name)} | {_md_cell(s.kind)} | {_md_cell(s.deploy_hint)} | {_md_cell(', '.join(s.routes_hot[:5]))} |")
    L.append("")
    L.append("## Top 10 flags\n")
    L.append("| # | flag | kind | load | why (evidence) | when off |\n|---|---|---|---|---|---|")
    for i, f in enumerate(top, 1):
        L.append(f"| {i} | `{f.flag_name}`{' (child of heavy_features)' if f in children else ''} | {f.kind} | "
                 f"{f.load_impact} | {_md_cell(f.title)}: {_md_cell(', '.join(f.evidence[:3]))} | {_md_cell(f.fallback_suggestion)} |")
    L.append(f"\nAll {len(flags_spec['features'])} flags written to `flagsmith/flags.yaml`: "
             + ", ".join(f"`{x['name']}`" for x in flags_spec["features"]) + ".\n")
    L.append("## Database\n")
    db = result.database
    L.append(f"- Engines: {', '.join(db.engines) or 'none found'}")
    if db.pool_settings:
        L.append("- Pool settings found: " + "; ".join(f"`{p.name}={p.value}` ({p.evidence})" for p in db.pool_settings[:8]))
    else:
        L.append("- Pool settings found: none (Insidious starts with `db_pool` = pool_size 5, timeout 2 s)")
    L.append(f"- Read-heavy paths (read-replica candidates): {', '.join(db.read_heavy_paths[:10]) or 'none identified'}\n")
    L.append("## Recommended SLOs\n")
    L.append("| route | p95 target | error budget |\n|---|---|---|")
    for s in result.slos:
        L.append(f"| `{_md_cell(s.route)}` | {int(s.p95_ms)} ms | {s.error_rate:.0%} |")
    L.append(f"\n## Scaling\n\nmin {result.scaling.min_replicas}, max {result.scaling.max_replicas} replicas. {result.scaling.notes}\n")
    if result.risks:
        L.append("## Risks for sale night\n")
        L += [f"- {r}" for r in result.risks]
        L.append("")
    L.append("## What Insidious will do on sale day\n")
    checkout = next((s.route for s in result.slos if re.search(r"checkout|order|pay|cart", s.route)), result.slos[0].route if result.slos else "/")
    L.append(f"1. **Before the sale**: `insidious rehearse` load-tests twice (with `{PARENT_FLAG}` on and off) and "
             f"reports how many more shoppers the store handles when heavy features are shed.")
    L.append(f"2. **Watch**: guardian checks `{checkout}` p95 and error rate every 5 s against the SLOs above"
             + (" and the database pool (≥90% busy or waits > 0.25 s)." if db.engines else "."))
    L.append(f"3. **First lever**: turn off `{PARENT_FLAG}`, which sheds "
             + (", ".join(f"`{f.flag_name}`" for f in children) or "nothing yet (add degradable features)")
             + "; shoppers get the cheaper fallbacks listed above. Big carts (`high_value_cart`) keep everything.")
    L.append(f"4. **Second lever**: scale to {min(result.scaling.max_replicas, 3)}–{result.scaling.max_replicas} replicas"
             + (", grow the DB pool live (`db_pool`), and if reads dominate, add a read replica and flip `read_from_replica`." if db.engines else "."))
    L.append("5. **Ask first**: every change goes to the founder on WhatsApp in plain words (\"Shall I go ahead?\"); "
             "nothing changes until they approve. Afterwards `insidious reset` restores every flag.\n")
    L.append("## Safety\n")
    L.append(ev.exclusion_notice() + ". `.env*` files, keys, certificates and secret files are skipped by name and "
             "never opened; secret-looking lines are redacted before any analysis.\n")
    L.append("## Next steps\n")
    L.append("- Review `insidious.yaml` (founder name, app URL, SLO routes).")
    L.append("- `insidious scan <repo> --apply` creates these flags in Flagsmith (idempotent; prints a plan when "
             "FLAGSMITH_* keys are missing).")
    L.append("- Wrap each heavy feature with the Insidious SDK (`flags.enabled(\"<flag>\")`) so it can be shed live.")
    return "\n".join(L) + "\n"


def write_outputs(out_dir: Path, result: ScanResult, ev: Evidence, meta: dict[str, Any]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flagsmith").mkdir(exist_ok=True)
    (out_dir / "docs").mkdir(exist_ok=True)
    cfg = build_insidious_config(result, ev)
    spec = build_flags_spec(result)
    header = (f"# Insidious config for {ev.repo_name}. Written by `insidious scan` ({meta['analysis']}) at "
              f"{meta['generated_at']}.\n# Review founder, app.url and SLO routes before `insidious init`.\n")
    paths = {"config": out_dir / "insidious.yaml", "flags": out_dir / "flagsmith" / "flags.yaml",
             "report": out_dir / "docs" / "scan-report.md"}
    paths["config"].write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=120))
    paths["flags"].write_text(
        f"# Desired Flagsmith state for {ev.repo_name}, discovered by `insidious scan`. Apply with "
        "`insidious scan --apply` or `insidious init`.\n# Do NOT enable Feature Versioning v2 on the environment.\n"
        + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120))
    paths["report"].write_text(build_report(result, ev, meta, spec))
    return paths


# ===================================================================== orchestration
@dataclass
class ScanOutcome:
    evidence: Evidence
    result: ScanResult
    analysis: str          # "claude (model)" | "heuristic (mock)" | "heuristic (fallback: ...)"
    paths: dict[str, Path]
    flags_spec: dict[str, Any]
    payload_chars: int


def resolve_provider(provider: str | None) -> str:
    if provider:
        return provider
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock"


def run_scan(repo: Path, out_dir: Path, provider: str = "mock", model: str = DEFAULT_MODEL, max_files: int = 400,
             client: Any = None, log=lambda s: None) -> ScanOutcome:
    ev = Scanner(repo, max_files=max_files, exclude_dir=out_dir).run()
    payload = build_payload(ev)
    text = payload_text(payload)
    if provider == "mock":
        result, analysis = heuristic_analysis(ev), "heuristic (mock provider, no network)"
    else:
        try:
            result = analyze_with_claude(payload, provider, model, client=client, log=log)
            analysis = f"claude ({provider}, {model})"
        except ClaudeScanError as e:
            log(f"Claude analysis failed: {e}; using the heuristic analyser")
            result, analysis = heuristic_analysis(ev), f"heuristic (fallback: {e})"
    meta = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "analysis": analysis}
    paths = write_outputs(out_dir, result, ev, meta)
    return ScanOutcome(ev, result, analysis, paths, build_flags_spec(result), len(text))
