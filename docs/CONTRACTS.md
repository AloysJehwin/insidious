# Insidious shared contracts

Every component must follow this file. Read `docs/ARCHITECTURE.md` for the why. If you change a contract, change it here too.

## Conventions

- Python 3.12 everywhere. FastAPI + uvicorn for HTTP services, `httpx` (async) for outbound HTTP, `pydantic` v2 models, `pytest` + `pytest-asyncio` + `respx` for tests.
- Each service lives in `services/<name>/` with `app/` (code), `tests/`, `requirements.txt`, `Dockerfile`.
- **Every Dockerfile is built with the repo root as context**: `docker build -f services/<name>/Dockerfile -t insidious/<name>:dev .` (so the store can `pip install ./sdk`). Base image `python:3.12-slim`. ops-bot also installs `ffmpeg` via apt.
- Configuration comes only from environment variables (names in `.env.example`) plus `insidious.yaml` (path in env `INSIDIOUS_CONFIG`, default `/config/insidious.yaml`, falling back to `./insidious.yaml`).
- Logging: one JSON line per event to stdout (`{"ts":..., "svc":..., "event":..., ...}`), no secrets in logs.
- Mock/dry-run switches must exist so everything runs with no external accounts: `SARVAM_MOCK=1`, `NUDGEBEE_DRY_RUN=1`, `WHATSAPP_DRY_RUN=1`. If a Flagsmith key is missing, the SDK falls back to defaults from `flagsmith/flags.yaml` (offline mode) and logs a warning.
- Health: every service exposes `GET /healthz` → `{"ok": true}`.

## Services, ports and DNS names

| Service | Port | Compose / k8s DNS | k8s namespace |
|---|---|---|---|
| store-api (Jeffy store + `/ops` cockpit) | 8000 | `store-api` | `store` |
| payments-mock | 8001 | `payments-mock` | `store` |
| guardian | 8002 | `guardian` | `insidious` |
| ops-bot | 8003 | `ops-bot` | `insidious` |
| prometheus | 9090 | `prometheus` | (NudgeBee-installed one on kind; compose runs its own) |

In k8s, cross-namespace URLs are `http://<svc>.<ns>.svc.cluster.local:<port>`; set them via the `*_URL` env vars.

## Prometheus metrics (store-api, emitted by the SDK middleware)

- `http_request_duration_seconds` histogram, labels `route` (the route template, e.g. `/product/{id}`), `method`, `status`. Buckets: `0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1, 1.5, 2, 3, 5, 8`.
- `http_requests_total` counter, labels `route`, `method`, `status`.
- `insidious_feature_shed_total` counter, label `feature` (incremented each time a degradable feature returns its fallback).
- `insidious_flag_state` gauge, label `flag` (1/0 for boolean flags as last evaluated at environment level).
- `insidious_orders_total` counter, labels `checkout_flow`, `gateway`.
- `/metrics` on port 8000. Pods carry annotations `prometheus.io/scrape: "true"`, `prometheus.io/port: "8000"`, `prometheus.io/path: /metrics`.

Guardian's p95 query (route label exact match):
```
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{route="/checkout"}[30s])) by (le))
```
Error rate: `sum(rate(http_requests_total{route="/checkout",status=~"5.."}[30s])) / clamp_min(sum(rate(http_requests_total{route="/checkout"}[30s])), 1e-9)`
RPS: `sum(rate(http_requests_total[30s]))`

## Flags (names are fixed; see `flagsmith/flags.yaml`)

`heavy_features` (parent) → `recommendations`, `hd_images`, `live_reviews`; `checkout_flow` (`full`|`lite`); `payment_gateway` (`razorpay`|`cashfree`); `ops_voice` (JSON); `festive_collection`. Segment `high_value_cart` uses trait `cart_value` (number) > 2000 and overrides `heavy_features` to enabled.

Identity for shoppers = session id cookie `fk_sid`. Traits sent on evaluation: `cart_value` (int rupees). Identity for the founder = `founder:<FOUNDER_PHONE>`.

## HTTP contracts between components

All internal POSTs from NudgeBee to ops-bot carry header `X-Insidious-Token: $INSIDIOUS_INTERNAL_TOKEN`. ops-bot rejects a wrong or missing token with 401.

### store-api (beyond the shop pages)

- JSON API for load tests: `GET /api/products`, `GET /api/products/{id}`, `GET /api/recommendations?pid=`, `POST /api/cart` `{"product_id": 3, "qty": 1}`, `POST /api/checkout` `{}` (uses the `fk_sid` cookie cart; if the cart is empty it adds one random product so k6 can call checkout directly). Checkout with `checkout_flow=full` also computes "frequently bought together" recommendations (so shedding `recommendations` speeds checkout); `lite` skips them.
- `GET /api/flags` → environment-level flag states `{"heavy_features": true, "recommendations": true, "hd_images": true, "live_reviews": true, "checkout_flow": "full", "payment_gateway": "razorpay", "festive_collection": false, "source": "flagsmith|offline"}`. guardian reads this for `/api/state.flags`.
- `POST /api/dev/flags` `{"heavy_features": false}` → sets local overrides on top of whatever Flagsmith returns. **Only enabled when `INSIDIOUS_DEV=1`**; returns 404 otherwise. Used by the CLI's rehearse pass 2 when no Flagsmith org key is configured.
- `GET /ops` cockpit page; `GET /ops/state` proxies `GUARDIAN_URL/api/state` (avoids CORS).
- CPU cost knob: env `HEAVY_WORK_MS` (default 40) = CPU milliseconds per recommendations computation. Run uvicorn with 1 worker per pod so one pod saturates quickly and scaling to 3 pods visibly helps.

### guardian replicas source

guardian reads the `store-api` Deployment's `status.availableReplicas` from the Kubernetes API when running in-cluster (service account `guardian` with get/list on `deployments` in namespace `store`; token at `/var/run/secrets/kubernetes.io/serviceaccount/token`). Outside k8s it falls back to `count(up{job="store-api"} == 1)` from Prometheus, else `null`.

### guardian

`GET /api/state` →
```json
{
  "armed": true,
  "slo": {"/checkout": {"p95_ms": 1840.0, "error_rate": 0.0, "target_p95_ms": 800, "breaching": true, "breach_count": 3}},
  "rps": 420.5,
  "replicas": {"store-api": 1},
  "flags": {"heavy_features": true, "recommendations": true, "payment_gateway": "razorpay"},
  "incident": {"id": "slo-checkout-p95-1732545600", "status": "firing", "since": "2026-11-25T14:02:05Z"},
  "last_execution": {"workflow": "sale-incident-guard", "execution_id": "...", "status": "RUNNING", "tasks": [{"id": "...", "type": "core.approval", "status": "WAITING"}]},
  "history": [{"ts": "2026-11-25T14:02:00Z", "p95_ms": {"/checkout": 1840.0}, "rps": 420.5}],
  "feed": [{"ts": "...", "source": "flagsmith|nudgebee|guardian|ops-bot", "text": "heavy_features turned OFF by NudgeBee"}]
}
```
`history` keeps the last 180 samples (15 min at 5 s). `feed` keeps the last 50 entries.

`POST /api/arm` `{"armed": true}` → `{"armed": true}`
`POST /api/feed` `{"source": "...", "text": "..."}` → `{"ok": true}` (ops-bot posts Flagsmith webhook events and approvals here)
`POST /api/rehearse/sample` `{"label": "pass1|pass2", "target_rps": 300}` → `{"p95_ms": {...}, "error_rate": {...}, "rps": 298.1}` (CLI asks guardian to take a measurement right now)

**Event Webhook payload guardian sends to NudgeBee** (`POST $NUDGEBEE_EVENT_WEBHOOK_URL`, header `Authorization: Bearer <token from the URL>` is optional):
```json
{
  "id": "slo-checkout-p95-<unix_ts_of_first_breach>",
  "fingerprint": "jeffy/store-api/checkout-p95",
  "status": "firing",
  "title": "Checkout p95 1840ms breaches 800ms SLO at 420 rps",
  "rule_name": "insidious-slo",
  "severity": "high",
  "subject": {"name": "store-api", "type": "Deployment", "namespace": "store"},
  "labels": {"route": "/checkout", "p95_ms": "1840", "rps": "420", "heavy_features": "on", "capacity_gain_if_shed": "+62%"},
  "description": "..."
}
```
On recovery the same `id` and `fingerprint` with `"status": "resolved"`. `capacity_gain_if_shed` comes from the latest `reports/readiness-*.json` if present.

### ops-bot

`GET /whatsapp/webhook` Meta verification (`hub.mode`, `hub.verify_token`, `hub.challenge`).
`POST /whatsapp/webhook` Meta events (verify `X-Hub-Signature-256` with `WHATSAPP_APP_SECRET`; 200 immediately; process in background; dedupe by message id).

`POST /nb/approval-request` (from a NudgeBee HTTP task) →
```json
{
  "execution_id": "optional, if the workflow can template it",
  "task_id": "optional",
  "workflow_id": "optional",
  "action": "shed_heavy_features|scale_out|failover_payment|ignore",
  "summary_en": "RCA text from NudgeBee",
  "numbers": {"p95_ms": 1840, "rps": 420, "gain": "+62%", "replicas_to": 3}
}
```
→ `{"ok": true, "pending_id": "..."}`. ops-bot stores the pending approval, sends the founder a voice note + text + Approve/Reject buttons. If `execution_id`/`task_id` are missing, ops-bot finds the waiting `core.approval` task by polling `workflow_list_executions` for `workflow_id` (or `NUDGEBEE_WF_INCIDENT_ID`) then `workflow_get_execution`.

`POST /nb/notify` `{"text_en": "...", "severity": "info|warning|critical", "voice": true}` → `{"ok": true}`. Sends the founder a translated text and, if `voice`, a voice note.

`POST /flagsmith/webhook` Flagsmith environment webhook (verify `X-Flagsmith-Signature` HMAC-SHA256 hex of the raw body with `FLAGSMITH_WEBHOOK_SECRET`). Forwards a one-line summary to guardian `/api/feed` and texts the founder.

`POST /api/brief` `{"kind": "readiness", "report": {...readiness json...}}` (from the CLI) → sends a short voice brief.

`GET /api/pending` → list of pending approvals (for the cockpit and debugging).

### NudgeBee GraphQL (used by ops-bot and CLI; base `$NUDGEBEE_URL/api/graphql`, header `Authorization: Bearer $NUDGEBEE_API_TOKEN`; errors arrive as HTTP 200 with an `errors` array)

```graphql
mutation { workflow_execute(request: {account_id: $acc, id: $workflowId, inputs: {...}}) { id execution_id } }
query { workflow_get_execution(request: {account_id: $acc, id: $executionId, workflow_id: $workflowId}) { id status start_time close_time inputs workflow_result error tasks { id type status input output error attempt children } } }
mutation { workflow_complete_approval(request: {execution_id: $e, task_id: $t, status: "approved", comments: "..."}) { status message } }
mutation { ai_execute_investigation(request: {account_id: $acc, query: "...", session_id: "...", async: true}) { ... } }
query { ai_get_conversation_v3(request: {account_id: $acc, session_id: "..."}) { conversation { id status } messages { response message_type } agents { agent_name status response } tool_calls { tool_name parameters } } }
```
These shapes come from NudgeBee's `nbctl` CLI source and are not in the public docs. Use GraphQL variables, keep every call behind one small client class, and log the raw `errors` array on failure.

### Flagsmith Admin API (used by CLI `init` and NudgeBee HTTP tasks)

Base `$FLAGSMITH_ADMIN_URL`, header `Authorization: Api-Key $FLAGSMITH_ORG_API_KEY`. Toggling an environment-level flag (legacy featurestate endpoints; work only while Feature Versioning v2 is OFF):
1. `GET /environments/{environment_key}/featurestates/?feature_name=<name>` → find the environment-level feature state (no `identity`, no `feature_segment`) and its `id`.
2. `PATCH /environments/{environment_key}/featurestates/{id}/` with `{"enabled": false}` (or `{"feature_state_value": "cashfree"}` for remote config).
Verify these paths against the Swagger at `https://api.flagsmith.com/api/v1/docs/` before relying on them; keep them in one helper module.

## Readiness report (`reports/readiness-<YYYYMMDD-HHMM>.json`, written by `insidious rehearse`)

```json
{
  "app": "jeffy",
  "generated_at": "2026-11-25T13:40:00Z",
  "slo": {"route": "/checkout", "p95_ms": 800},
  "pass1": {"label": "heavy_features on", "breaking_rps": 420, "steps": [{"target_rps": 50, "rps": 49.8, "p95_ms": {"/checkout": 120}, "error_rate": {"/checkout": 0}}]},
  "pass2": {"label": "heavy_features off", "breaking_rps": 680, "steps": [...]},
  "capacity_gain_if_shed": "+62%",
  "recommendation": "Arm guard with heavy_features as the first lever; pre-scale to 3 replicas 30 min before the sale."
}
```
`breaking_rps` = the first step's target RPS where `/checkout` p95 > SLO or error rate > SLO; `null` if never breached.

---

# v2 contracts: Claude ops agent, database scaling, codebase scan

The founder is non-technical. Every message to the founder says what is happening, the recommended action and its cost in plain words, and asks "shall I go ahead?". Nothing changes until the founder approves (autopilot is off by default).

## Service `agent` (Claude-powered planner + executor)

Namespace `insidious`, port **8004**, DNS `agent` (compose) / `agent.insidious.svc.cluster.local` (k8s). Env `AGENT_URL` points at it from guardian and ops-bot.

Endpoints (all POSTs from other services carry `X-Insidious-Token`):
- `POST /api/incident` `{"source": "guardian", "incident": {...guardian incident...}, "state": {...guardian /api/state...}}` → `202 {"plan_id": "plan-..."}`. Collects telemetry, asks Claude for a plan, stores it, sends it to ops-bot `/nb/approval-request` with `plan_id` + `options`.
- `GET /api/plans`, `GET /api/plans/{id}` → plan JSON.
- `POST /api/plans/{id}/decision` `{"option": "recommended|alt1|alt2|reject", "channel": "whatsapp-button|whatsapp-voice|cli", "actor": "founder", "comments": "..."}` → executes the chosen option's actions in order, returns `{"status": "done|failed|rejected", "results": [...]}` and posts each step to guardian `/api/feed`.
- `POST /api/ask` `{"question": "...", "language": "hi-IN"}` → `{"answer_en": "..."}` (Claude answers using the same telemetry; ops-bot translates/speaks it).
- `GET /healthz`.

Plan JSON:
```json
{
  "id": "plan-20261125-140205-ab12", "created_at": "...", "incident_id": "slo-checkout-p95-...",
  "status": "pending_approval|executing|done|rejected|failed",
  "founder_summary_en": "Lots of shoppers right now and checkout is slow. I suggest pausing product suggestions and adding 2 more servers.",
  "technical_summary": "store-api p95 1.9s at 55 rps; CPU 98% of 500m limit; db pool 5/5 in use, wait p95 0.4s ...",
  "options": [
    {"key": "recommended", "title_en": "Pause suggestions + add 2 servers", "impact_en": "Checkout fast again in about a minute", "cost_en": "About ₹12 more per hour",
     "actions": [{"type": "flagsmith.set_flag", "params": {"flag": "heavy_features", "enabled": false}},
                 {"type": "k8s.scale", "params": {"namespace": "store", "deployment": "store-api", "replicas": 3}}]},
    {"key": "alt1", "title_en": "Add a copy of the database for browsing", "impact_en": "...", "cost_en": "...", "actions": [...]},
    {"key": "reject", "title_en": "Do nothing for now", "impact_en": "", "cost_en": "", "actions": []}
  ],
  "results": [{"type": "...", "ok": true, "detail": "...", "ts": "..."}]
}
```
At most 3 options; the last is always `reject`.

### Action catalog (the ONLY actions the agent may propose; executors validate params against guardrails)

| type | params | does |
|---|---|---|
| `flagsmith.set_flag` | `flag`, `enabled` | environment-level toggle via Admin API |
| `flagsmith.set_value` | `flag`, `value` (string; JSON as string) | environment-level remote config value |
| `k8s.scale` | `namespace`, `deployment`, `replicas` | patch deployments/scale (≤ `max_replicas`) |
| `k8s.set_resources` | `namespace`, `deployment`, `container`, `cpu_limit`, `memory_limit` (+ optional requests) | patch container resources ("more memory/CPU") |
| `db.set_pool` | `pool_size`, `max_overflow`, `timeout_s` | writes Flagsmith remote config `db_pool` (store resizes its pool live) |
| `aws.rds.create_read_replica` | `source_db_id`, `replica_id`, `instance_class` | starts an RDS read replica; a background watcher waits for `available`, then sets Flagsmith `db_read_url` and turns `read_from_replica` ON, and notifies the founder |
| `aws.rds.modify_instance_class` | `db_id`, `instance_class` | vertical DB scaling (apply immediately) |
| `nudgebee.run_workflow` | `workflow_id`, `inputs` | run a NudgeBee workflow (audit/remediation) |
| `notify.founder` | `text_en` | POST ops-bot `/nb/notify` |

Guardrails in `insidious.yaml` → `agent.guardrails`: `max_replicas: 5`, `allowed_namespaces: [store]`, `allowed_db_instance_classes: [db.t4g.micro, db.t4g.small, db.t4g.medium]`, `max_memory_limit: 2Gi`, `max_cpu_limit: "2"`, `rds_id_prefix: [insidious-, festivekit-]` (festivekit-* = the pre-rename RDS instances). Anything outside → action rejected with a clear message.

### Claude

- Provider `anthropic` (env `ANTHROPIC_API_KEY`, official `anthropic` Python SDK) or `bedrock` (AWS credentials from the instance role, region `AWS_REGION`). `AGENT_MOCK=1` → deterministic rule-based planner, no network (tests, compose, demo without a key).
- Model and behaviour come from Flagsmith remote config `agent_config` (JSON): `{"provider":"anthropic","model":"claude-sonnet-5-5","autopilot":false,"max_actions":4,"language_hint":"plain, non-technical"}`; env `AGENT_MODEL` is the fallback. Flag `ai_agent` OFF = agent only notifies (no plans) — the AI kill switch.
- The plan must come back as structured output (tool use / JSON schema), validated with pydantic; on invalid output retry once, then fall back to the rule-based planner.

### Telemetry the agent collects for each incident
guardian `/api/state`; Kubernetes (in-cluster SA): deployments in `store` (replicas, container resources), pods (phase, restarts, last termination reason e.g. OOMKilled), `metrics.k8s.io` pod CPU/memory; Prometheus DB pool metrics; last 50 log lines containing ERROR/Traceback from store-api pods; RDS instance status/class (if `DATABASE_RDS_ID` set); optional NudgeBee RCA (`ai_execute_investigation`, ≤60 s, skipped on error); newest readiness report.

## Database layer (store + SDK + guardian)

- Store uses PostgreSQL through SQLAlchemy 2: `DATABASE_URL` (primary). Reads (catalog, product, recommendations source data) go to the replica when flag `read_from_replica` is ON and remote config `db_read_url` is non-empty; writes (cart/orders) always go to primary. If `DATABASE_URL` is unset → SQLite fallback (tests/compose still work).
- Pool size from Flagsmith remote config `db_pool` (JSON `{"pool_size":5,"max_overflow":0,"timeout_s":2}`); the engine is rebuilt when the config changes (checked at most every 5 s).
- Schema + seed (products, orders) created on startup if missing (idempotent).
- SDK module `insidious.db` instruments any SQLAlchemy engine: `insidious_db_pool_size{db}`, `insidious_db_pool_in_use{db}`, `insidious_db_pool_wait_seconds` histogram `{db}`, `insidious_db_query_seconds` histogram `{db,op}`; `db` label = `primary|replica`.
- Env `DB_QUERY_WORK_MS` (default 5) adds a realistic per-query cost (`pg_sleep` on Postgres, `time.sleep` on SQLite) so pool saturation shows up under demo load.
- guardian DB SLO: pool utilisation `in_use/size` ≥ 0.9 or wait p95 > 0.25 s for 3 samples → incident with `rule_name: insidious-db`, route key `db:primary`. guardian POSTs every fired incident to `AGENT_URL/api/incident` (best effort, 2 s timeout) in addition to the NudgeBee event.

New flags in `flagsmith/flags.yaml`: `read_from_replica` (FLAG, off), `db_read_url` (CONFIG, ""), `db_pool` (CONFIG JSON), `agent_config` (CONFIG JSON), `ai_agent` (FLAG, on).

## ops-bot: multi-option approvals

- `/nb/approval-request` also accepts `plan_id` and `options` `[{"key","title_en"}]` (≤3). WhatsApp reply buttons get ids `opt:<pending_id>:<key>` (titles ≤20 chars after translation; full titles in the body text). The founder text uses `founder_summary` (or `summary_en`) translated with Sarvam, ending "Shall I go ahead?" / localized.
- Choosing an option (button, or voice/text like "haan", "pehla wala", "recommended", "nahi", "doosra") → `POST AGENT_URL/api/plans/{plan_id}/decision` `{option}`; if the pending item also has a NudgeBee `execution_id`, also call `workflow_complete_approval` (approved unless `reject`, comments `option=<key>`). Read back the result ("Done: suggestions paused, 2 servers added").
- `/nb/notify` unchanged; agent uses it for progress ("Database copy is ready, shoppers now read from it").

## `insidious scan` (CLI, Claude-powered)

`insidious scan <repo_path> [--out <dir>] [--apply] [--provider anthropic|bedrock|mock] [--max-files 400]` analyses an existing application and writes `insidious.yaml` (services, routes, SLO suggestions, degradable features, DB config, scaling policy) + `flagsmith/flags.yaml` (discovered toggles as Flagsmith flags) + `docs/scan-report.md`. `--apply` creates the flags in Flagsmith (Admin API). It must never read or send `.env*` files, keys, certificates or anything matching common secret patterns.
