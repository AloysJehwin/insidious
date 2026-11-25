# Insidious: architecture and build plan

> Working names: **Insidious** is the product (SDK + CLI + ops bot). **Jeffy** is the demo customer: a mid-size, self-hosted D2C store. Rename freely.

**One line:** Insidious gets a self-hosted D2C store through festive sale night. Three commands plug it in. Flagsmith sheds load feature by feature, NudgeBee runs the incident loop on Kubernetes, and the founder runs ops from WhatsApp in Hindi or Tamil by voice, through Sarvam.

**Named user (for the video and README):** Ravi runs a 12-person jutti brand in Jaipur on his own store. Last Diwali his site went down at 8 PM on sale night. Nobody on the team is an SRE, and Ravi can't read Grafana, but he answers WhatsApp voice notes in Hindi within seconds.

Hacking started at 11:30, so design and code are both allowed now. Commit early and often: a clean git history proves rule 4.

---

## 1. What we are building (scope lock)

Three CLI commands, one ops bot, one demo store.

| Piece | What it does | In scope today | Out of scope |
|---|---|---|---|
| `insidious init` | Adds the SDK to the store, creates the Flagsmith flags and segments, applies the k8s manifests, checks that the NudgeBee workflows exist | Yes | Auto-detecting arbitrary stacks (we support one: Python/FastAPI) |
| `insidious rehearse` | Festive load test that finds the breaking point and measures **how many extra requests per second each flag buys**, then writes a readiness report | Yes | Full end-to-end test suites, CI/CD |
| `insidious guard` | Sale-day mode: arms the SLO watcher and triggers the pre-sale warm-up workflow | Yes | Multi-cluster, real cloud autoscaling |
| Ops bot (WhatsApp) | Founder's voice interface: alerts, plain-language explanations, approvals by voice or button, "why is it slow?" | Yes | Customer-facing support bot |
| Jeffy store | Demo D2C store with deliberately heavy endpoints | Yes | Real payments (mock gateways only, said on camera) |

**The unique technical idea to say out loud:** *flag-aware load testing*. `rehearse` runs the same load twice, once with heavy features on and once with them shed. That puts a price tag on every flag: "turning off recommendations buys you +60% checkout capacity". On sale night, NudgeBee uses those numbers to choose which flag to flip.

---

## 2. Architecture

```
                         ┌──────────────────────────── Founder (Ravi) ───────────────────────────┐
                         │  WhatsApp: voice notes in Hindi/Tamil, Approve/Reject buttons          │
                         └───────────────▲───────────────────────────────┬───────────────────────┘
                                         │ voice note + text (OGG/Opus)  │ voice note / button tap
                                  Meta WhatsApp Cloud API (test number)   │
                                         │                               ▼
                                   ngrok static domain ──► ingress (kind cluster)
┌──────────────────────────────────────── kind cluster ─────────────────────────────────────────────────┐
│  ns: insidious                                                                                        │
│  ┌──────────────────────────────┐      ┌────────────────────────────┐                                 │
│  │ ops-bot (FastAPI)            │      │ guardian (Python)           │                                 │
│  │ /whatsapp/webhook  ◄─ Meta   │      │ every 5 s: PromQL p95/errors│                                 │
│  │ /nb/approval-request ◄─ NB   │      │ 3 breaches → POST NudgeBee  │──► NudgeBee Event Webhook       │
│  │ /nb/notify          ◄─ NB    │      │ recovery   → status=resolved│        (cloud)                  │
│  │ /flagsmith/webhook  ◄─ FS    │      │ /api/state for cockpit      │                                 │
│  │ Sarvam: Saaras → 105b → Bulbul│     └──────────────┬─────────────┘                                 │
│  │ NudgeBee GraphQL client      │                     │ PromQL                                         │
│  └──────────────┬───────────────┘                     ▼                                                │
│                 │ GraphQL                  ┌────────────────────┐   ┌──────────────────────────┐      │
│                 ▼                          │ Prometheus          │◄──│ NudgeBee agent           │      │
│        NudgeBee cloud (workflows,          │ (installed by NB)   │   │ (inventory, events,      │      │
│        NuBi, approvals, RCA)               └─────────▲──────────┘   │  k8s.* tasks run here)   │      │
│                                                      │ /metrics      └──────────────────────────┘      │
│  ns: store                                           │                                                 │
│  ┌──────────────────────────────────────────────────┴─────┐   ┌───────────────────────┐              │
│  │ store-api "Jeffy" (FastAPI + insidious SDK)            │──►│ payments-mock         │              │
│  │ / , /product/{id}, /recommendations (CPU-heavy),        │   │ razorpay-mock /       │              │
│  │ /reviews, /cart, /checkout, /ops (cockpit), /metrics    │   │ cashfree-mock (chaos) │              │
│  │ Flagsmith: server-side key, LOCAL evaluation (5 s)      │   └───────────────────────┘              │
│  └─────────────────────────────────────────────────────────┘                                          │
│  k6 load generator (Job, run by `insidious rehearse` / `make festive-rush`)                           │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ flag reads (env doc every 5 s)                           │ flag writes (Admin API, org key)
        └──────────────────────── Flagsmith cloud ◄────────────────┘  from NudgeBee HTTP tasks
                                  │ environment webhook (FLAG_UPDATED) → ops-bot /flagsmith/webhook
```

### The four loops

**Loop 1: Rehearsal (before the sale)**
`insidious rehearse` → k6 ramps 50 → 800 rps in steps → guardian samples p95 and error rate per step → pass 1 with heavy features ON, pass 2 with `heavy_features` OFF → readiness report (markdown + JSON) → ops-bot sends Ravi a 20-second Hindi voice brief: "Your store breaks at 420 requests per second. Turning off recommendations raises that to 680."

**Loop 2: Sale-night incident (the hero loop)**
1. `make festive-rush` spikes traffic. Checkout p95 crosses the 800 ms SLO.
2. guardian sees 3 consecutive breaches (about 15 s) and POSTs to the **NudgeBee Event Webhook**, with `subject` pointing at the `store-api` Deployment.
3. NudgeBee creates the event and runs its **AI RCA** automatically.
4. Workflow `sale-incident-guard` fires on `investigation.completed`:
   - `llm.classify` picks one of `shed_heavy_features`, `scale_out`, `failover_payment`, `ignore`, using the RCA plus the readiness numbers.
   - An HTTP task POSTs to ops-bot `/nb/approval-request`.
   - `core.approval` pauses.
5. ops-bot turns the English RCA into a short Hindi explanation (sarvam-105b) and speaks it with Bulbul. Ravi gets a voice note plus **Approve / Reject** buttons.
6. Ravi taps Approve or says "haan karo". ops-bot calls `workflow_complete_approval`.
7. The workflow resumes:
   - An HTTP task calls the **Flagsmith Admin API** to turn `heavy_features` off.
   - The `high_value_cart` segment override keeps the full experience for big carts.
   - Optionally, `k8s.horizontal_rightsize` scales `store-api` from 1 to 3.
8. Within 5 s the store's local-eval SDK sees the flag. Recommendations return a cached fallback, and p95 recovers on the cockpit.
9. guardian sends `status: resolved`. The workflow's final HTTP task tells ops-bot to send Ravi "Sab theek hai" with the before/after numbers.

**Loop 3: Ask why (on demand)**
Ravi sends "site slow kyun hai?" → Saaras → intent `why` → ops-bot calls NudgeBee `ai_execute_investigation(async: true)` and polls `ai_get_conversation_v3`. Ravi sees text updates as NuBi calls tools ("checking pods…", "reading metrics…"), then gets the final answer as a Hindi voice note.

**Loop 4: Flags as the audit trail**
Every flag change (by NudgeBee, a human or the CLI) fires the Flagsmith environment webhook → ops-bot verifies the HMAC → the cockpit change feed updates, and Ravi gets a one-line text: "NudgeBee turned off recommendations at 20:03, approved by you."

---

## 3. Component specs

### 3.1 insidious SDK (Python package, used by the store)

```python
from insidious import Insidious
fk = Insidious.from_env()            # FLAGSMITH_SERVER_KEY, local eval, refresh 5 s
app.add_middleware(fk.metrics_middleware)   # Prometheus histogram by route + status

@fk.degradable("recommendations", fallback=cached_top_sellers)
def recommendations(user): ...        # skipped when the flag is off for this user

gateway = fk.config("payment_gateway", default="razorpay")
variant = fk.variant("checkout_flow", identity=session_id, traits={"cart_value": cart.total})
fk.track("order_placed", identity=session_id)   # for the A/B metric
```

- Uses the Flagsmith Python SDK with `enable_local_evaluation=True` and a 5-second refresh. **No per-request Flagsmith API calls**, so load tests can't burn the 50k-a-month free quota. Say this in the video.
- If flag dependencies aren't on the free plan, `degradable()` checks the parent `heavy_features` flag itself.

### 3.2 Jeffy store (`store-api`)

- FastAPI with Jinja templates. Pages: home (with recommendations), product, cart, checkout.
- `/recommendations` is CPU-heavy on purpose (a similarity loop over the catalogue). `/product/{id}` resizes images on the fly when `hd_images` is on.
- `/checkout` calls `payments-mock` using the gateway name from remote config.
- `/ops` is the **cockpit page** for the video. It polls guardian's `/api/state` every second and shows:
  - a p95 chart per route
  - error rate
  - replicas
  - current flag states
  - the NudgeBee execution timeline (from `workflow_get_execution`)
  - the Flagsmith change feed

### 3.3 guardian

- Every 5 s it runs `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{route="/checkout"}[30s])) by (le))` plus an error-rate query.
- The SLOs come from `insidious.yaml`. After 3 consecutive breaches it POSTs to the Event Webhook; after 3 consecutive healthy samples it sends `resolved`.
- It also drives `rehearse`'s measurement (per-step sampling) and serves `/api/state`.

### 3.4 ops-bot

- The Meta webhook handler checks `X-Hub-Signature-256`, skips message IDs it has already seen, returns 200 straight away and processes in the background.
- **Inbound voice:**
  1. `GET /{media-id}`, then download the file with the bearer token (the URL expires in 5 minutes).
  2. Saaras v4 `POST /speech-to-text` with `mode=codemix` and no `language_code`, so it auto-detects.
  3. Save the detected language for this founder.
- **Intent:** `sarvam-105b` with a strict `json_schema` returns `{intent: approve|reject|status|why|rehearse|restore, confidence}`. Voice approval is accepted only when exactly one approval is pending. Read back what was approved.
- **Outbound:**
  1. sarvam-105b writes 1–2 plain sentences in the founder's language from structured facts (RCA, numbers).
  2. Bulbul with **`model: "bulbul:v4-flash"` set explicitly** turns them into WAV.
  3. `ffmpeg -i in.wav -ac 1 -ar 48000 -c:a libopus -b:a 32k -application voip out.ogg` converts it.
  4. `POST /{PNID}/media`, then send `{"type":"audio","audio":{"id":…,"voice":true}}`, plus a text copy and Approve/Reject buttons.
- **Voice settings from Flagsmith:** the founder is a Flagsmith identity, with remote config `ops_voice` = `{speaker, pace, language}`. Change it in the dashboard and the next voice note changes.
- **NudgeBee bridge:** a GraphQL client for `workflow_complete_approval`, `workflow_get_execution`, `workflow_list_executions`, `ai_execute_investigation` and `ai_get_conversation_v3`.

### 3.5 CLI (`insidious`, Typer)

| Command | Steps |
|---|---|
| `init` | 1. Write `insidious.yaml` (SLOs, degradable features, founder phone, workflow IDs). 2. Create or update Flagsmith flags and segments through the Admin API (idempotent, from `flagsmith/flags.yaml`). 3. `kubectl apply -k deploy/k8s`. 4. Check the NudgeBee token and that the workflow IDs exist. 5. Print a green checklist. |
| `rehearse` | 1. Launch the k6 Job. 2. Pass 1 with heavy features on. 3. Flip `heavy_features` off. 4. Pass 2. 5. Restore the flag. 6. Write `reports/readiness-<date>.md` and JSON. 7. POST the summary to ops-bot for the voice brief. |
| `guard` | 1. Set guardian to armed. 2. Trigger the `pre-sale-warmup` workflow through `workflow_execute`. |
| `status` | Print flags, replicas, SLO state and the last NudgeBee execution. |

---

## 4. Flagsmith design (aim: the ₹1 lakh "best use" prize)

| Flag | Type | Role | Feature shown |
|---|---|---|---|
| `heavy_features` | boolean, parent | One switch that sheds everything heavy | **Flag dependencies** (parent → children) |
| `recommendations`, `hd_images`, `live_reviews` | boolean, children of `heavy_features` | Individually sheddable features | Kill switches |
| `high_value_cart` | segment: trait `cart_value` > 2000 | Segment override keeps `heavy_features` ON for big carts while everyone else is shed | **Segments + segment overrides on traits** |
| `checkout_flow` | multivariate `full` / `lite`, 50/50 | Real A/B test: does lite checkout convert better under load? Metric `order_placed` | **Multivariate + Experimentation** |
| `payment_gateway` | remote config string | `razorpay` → `cashfree` failover | **Remote config** |
| `ops_voice` | remote config JSON, on the founder's identity | Sarvam speaker, pace, language for ops voice notes | **Identity + remote config** |
| `festive_collection` | boolean with a % split segment | Progressive rollout of the new collection on sale day: 10% → 50% → 100% | **Percentage rollout** (stretch) |

- **Reads:** the store uses local evaluation through the server-side key.
- **Writes:** NudgeBee HTTP tasks call the Admin API with the **organisation API key**, kept in NudgeBee Secrets as `{{ Secrets.flagsmith_org_key }}`.
- **Environment webhook:** sends to ops-bot for the audit feed.
- **Do NOT enable Feature Versioning v2.** It's irreversible and breaks the featurestate write endpoints.

---

## 5. NudgeBee design

| Workflow | Trigger | Tasks |
|---|---|---|
| `pre-sale-warmup` | Schedule (cron in **UTC**) + Manual | `k8s.horizontal_rightsize` store-api → 3; HTTP Flagsmith: confirm `heavy_features` on; HTTP ops-bot `/nb/notify` "Sale in 30 min, scaled to 3" |
| `sale-incident-guard` | Event on `investigation.completed`, filter `rule_name = insidious-slo` | `llm.classify` (options: shed_heavy_features, scale_out, failover_payment, ignore) → HTTP ops-bot `/nb/approval-request` → `core.approval` (in-app) → `core.switch` on the chosen option → HTTP Flagsmith Admin API **or** `k8s.horizontal_rightsize` **or** HTTP Flagsmith `payment_gateway=cashfree` → HTTP ops-bot `/nb/notify` |
| `post-sale-report` | Manual | Read events → `llm.summary` → HTTP ops-bot `/nb/notify` (voice summary), plus cost and rightsizing recommendations if an hour of history exists by then |

- **Raise both the task and automation timeouts to 30 min.** The default of 5 min kills approvals.
- Export every workflow's JSON and a screenshot into `nudgebee/workflows/` for the README.

**Event Webhook payload (guardian → NudgeBee):**
```json
{
  "id": "slo-checkout-p95-1732545600",
  "fingerprint": "jeffy/store-api/checkout-p95",
  "status": "firing",
  "title": "Checkout p95 1840ms breaches 800ms SLO at 420 rps",
  "rule_name": "insidious-slo",
  "severity": "high",
  "subject": {"name": "store-api", "type": "Deployment", "namespace": "store"},
  "labels": {"route": "/checkout", "p95_ms": "1840", "rps": "420", "heavy_features": "on", "capacity_gain_if_shed": "+62%"},
  "description": "From insidious guardian. Readiness report: shedding heavy_features raised breaking point 420 → 680 rps."
}
```

**ops-bot → NudgeBee approval:**
```graphql
mutation { workflow_complete_approval(request: {execution_id: "...", task_id: "...", status: "approved", comments: "voice: haan karo (hi-IN, 0.97)"}) { status message } }
```

---

## 6. Repo layout

```
insidious/
├── sdk/insidious/            # SDK: flags.py, degrade.py, metrics.py, events.py
├── cli/                       # Typer CLI: init, rehearse, guard, status
├── services/
│   ├── store/                 # Jeffy store + /ops cockpit
│   ├── payments-mock/
│   ├── guardian/
│   └── ops-bot/               # WhatsApp + Sarvam + NudgeBee bridge
├── deploy/
│   ├── kind-config.yaml
│   └── k8s/                   # kustomize: namespaces, deployments, services, ingress, scrape config, secrets template
├── loadtest/                  # k6 scripts: rehearse.js, festive-rush.js
├── flagsmith/flags.yaml       # desired flag state, applied by `insidious init`
├── nudgebee/workflows/        # exported workflow JSON + screenshots
├── Makefile                   # up, init, rehearse, festive-rush (break-demo), reset, logs
├── .env.example
└── README.md                  # setup someone else can follow + AI-assistant disclosure
```

**Contracts so lanes can build in parallel** (agree on these in the first 30 minutes):
- `POST ops-bot /nb/approval-request` `{execution_id, task_id?, action, summary_en, numbers:{p95_ms, rps, gain}}`
- `POST ops-bot /nb/notify` `{text_en, severity}`
- `GET guardian /api/state` `{slo:{route:{p95_ms, err_rate, breaching}}, replicas, flags, last_execution}`
- `POST guardian /api/rehearse/step` `{step, rps}` (CLI ↔ guardian during rehearsal)

---

## 7. Build plan (today, IST)

Lanes assume 4 people. With 3, merge A and B. With 2, one person takes A + B and the other C + D.

| Lane | Owner | Owns |
|---|---|---|
| **A: Store + SDK** | | Jeffy store, insidious SDK, Flagsmith reads, metrics, cockpit UI |
| **B: Guardian + load + CLI** | | guardian, k6 scripts, `rehearse` and readiness report, `init`/`guard`/`status` |
| **C: NudgeBee + k8s** | | manifests, ingress, scrape config, the three workflows, Flagsmith Admin API calls from HTTP tasks |
| **D: WhatsApp ops bot** | | Meta webhook, Sarvam pipeline, approval bridge, NuBi "why" loop |

| Time | Everyone / milestone | A | B | C | D |
|---|---|---|---|---|---|
| **11:45–12:15** | Repo skeleton, `.env.example`, contracts above, **enable the Flagsmith Experimentation warehouse now** (first results can take hours) | Create flags and segments in the dashboard | k6 hello-world against a stub | Namespaces, ingress, ngrok → ops-bot route | Meta webhook verify + echo text |
| **12:15–1:00** | **Spikes:** settle the open questions in §8 | Store pages + heavy `/recommendations` + `/metrics` | guardian PromQL against real metrics | `curl` the Event Webhook → event + RCA appear; time the RCA | Voice note → Saaras → Bulbul → voice note back (round trip) |
| **1:00–1:30** | Lunch, staggered so one person always watches the cluster | | | | |
| **1:30–2:30** | **M1 at 2:30: Loop 2 works end to end once**, even if ugly | SDK `degradable()` + local eval at 5 s | guardian → Event Webhook firing/resolved | `sale-incident-guard` workflow with HTTP → Flagsmith | Approval request → buttons → `workflow_complete_approval` |
| **2:30–3:15** | **M2: `make festive-rush` heals itself 3 times in a row** | Cockpit `/ops` with timeline and change feed | `rehearse` two-pass + readiness report | `pre-sale-warmup` + `k8s.horizontal_rightsize`; raise timeouts | Hindi/Tamil explanations; voice approval; Flagsmith webhook feed |
| **3:15–3:45** | **M3: feature freeze at 3:45** | `checkout_flow` A/B + `order_placed` tracking | `init` idempotent + checklist output | `post-sale-report`; export workflow JSON + screenshots | NuBi "why" loop with live tool-call updates |
| **3:45–4:15** | **Record the demo video** (3+ takes, keep the best). Run the README setup on a clean terminal | | | | |
| **4:15–4:45** | README, architecture image, AI disclosure, push, upload video | | | | |
| **4:45–5:00** | **Submit.** Buffer | | | | |

**Cut lines:**
- **Behind at 2:30:** drop payment failover, `festive_collection` rollout and the A/B test. Keep Loop 2 plus a one-pass `rehearse`.
- **Behind at 3:15:** drop the NuBi "why" loop and `post-sale-report`. Use buttons only, not voice approval.
- **Never cut:** WhatsApp voice in and out in Hindi, the NudgeBee RCA plus approval, the Flagsmith flip, and visible p95 recovery on the cockpit.

---

## 8. Open questions to settle in the first hour

| # | Question | How to check | Fallback |
|---|---|---|---|
| 1 | Exact Flagsmith Admin API call to toggle a flag and edit a segment override | Swagger at `api.flagsmith.com/api/v1/docs/`; test with `curl` and the org key | Flagsmith MCP server or CLI from an ops-bot endpoint |
| 2 | Are flag dependencies and environment webhooks on the free plan? | Try in the dashboard | SDK enforces the parent; ops-bot polls the audit log for the feed |
| 3 | Do NudgeBee HTTP tasks run from the cloud or the in-cluster agent? | Point one at the ngrok URL and watch the logs | ngrok covers both; for Flagsmith it doesn't matter (public API) |
| 4 | How does the bot learn the approval `execution_id`/`task_id`? | Look for a template variable for the run ID in task inputs | ops-bot polls `workflow_list_executions` + `workflow_get_execution` for a pending `core.approval` task |
| 5 | Custom approval options through the API | Try `status` other than approved/rejected | Approve/reject only; put the choice in `comments` and branch with `core.switch` |
| 6 | How long the RCA takes on kind | Time three `curl` events | Pre-run one incident; show an on-screen clock in the video, don't fake it |
| 7 | Meta interactive buttons on the test number | Send one | Text reply "1 = approve, 2 = reject" plus voice |
| 8 | Experimentation event API and identifier rules | Flagsmith docs; ask Gagan at the booth | Log `order_placed` ourselves and show conversion per variant on the cockpit |

---

## 9. Demo video script (3:00)

| Time | Scene | Proof |
|---|---|---|
| 0:00–0:15 | Ravi's story over his store: "Diwali sale night, 8 PM, site down. No SRE. He lives on WhatsApp." | Named user and moment |
| 0:15–0:40 | `insidious init` in a terminal: green checklist; flags appear in Flagsmith; NudgeBee workflows listed | "Three commands" claim |
| 0:40–1:05 | `insidious rehearse`: chart of two passes; report says "breaks at 420 rps; shedding heavy features → 680 rps"; Ravi's phone gets a Hindi voice brief | Flag-aware load testing |
| 1:05–1:45 | `make festive-rush`: cockpit p95 goes red; NudgeBee event + RCA on screen; Ravi's phone gets a Hindi voice note with Approve/Reject | NudgeBee RCA → Sarvam voice |
| 1:45–2:15 | Ravi says "haan karo". The workflow canvas resumes, `heavy_features` flips in Flagsmith (audit log shows NudgeBee's key), big carts keep full experience, p95 drops back to green, pods 1 → 3 | All three tools in one moment |
| 2:15–2:40 | Ravi asks "abhi kya haal hai?"; NuBi's live tool calls, then a voice answer. Flagsmith A/B: lite vs full checkout conversion | NuBi + experiment |
| 2:40–3:00 | Architecture image. "Remove Sarvam: Ravi can't run his own ops. Remove Flagsmith: you can only add servers, not shed load. Remove NudgeBee: someone has to be awake at a terminal." Repo URL | Remove-one-tool test |

Say on camera: payments are mocked, and kind runs on one laptop, so scaling is real but small.

---

## 10. Budgets and limits to respect

- **Sarvam:** 60 req/min shared by the whole account; ₹100 credit. Only the founder's messages hit Sarvam, never the load tests. Keep voice replies under about 200 characters (about ₹0.40 each with v4-flash) and cache fixed phrases.
- **Flagsmith free plan:** 50k requests a month and 1 seat. Local evaluation keeps the store off the quota; teammates without dashboard seats use the org API key.
- **NudgeBee:** 30-min timeouts on approval workflows. Prometheus' built-in alerts are too slow on kind, so guardian posts events directly.
- **WhatsApp:** only the 5 verified phones; Ravi's phone must message the bot today (24-hour window). Voice notes under 512 KB (about 90 s).

---

## v2: what is deployed now (Claude agent, RDS, k3s on EC2)

v1 above planned NudgeBee workflows as the incident loop on kind. v2 keeps every v1 component and adds a Claude ops
agent as the planner/executor, a real database layer and a public deployment. Contracts for v2 are in
[CONTRACTS.md](CONTRACTS.md#v2-contracts-claude-ops-agent-database-scaling-codebase-scan). The demo customer is now
spelled **Jeffi** in the docs (code and metrics still say `jeffy`).

### Topology

```
Founder's WhatsApp ──► Meta Cloud API (test number) ──► https://festive.aloysjehwin.com/whatsapp/webhook
Shoppers / judges  ──► https://festive.aloysjehwin.com (store) and /ops (cockpit)
        │
Cloudflare (proxied DNS, TLS) ──► AWS EC2 t3.xlarge, ap-south-1, SG: 443 from Cloudflare ranges only
        │
k3s (single node) ── Traefik ingress
  ns store:      store-api (FastAPI + insidious SDK), payments-mock
  ns insidious: guardian, ops-bot, agent (:8004), Prometheus
  NudgeBee agent: Prometheus, Loki, ClickHouse, node agent
        │
AWS RDS PostgreSQL festivekit-primary (db.t4g.micro); optional read replica created by the agent
IAM: instance profile with role festivekit-agent, RDS actions limited to festivekit-* instances
```

### The v2 incident loop

1. **Detect.** guardian samples checkout p95, error rate and DB pool use every 5 s. Three breaching samples → one
   incident, posted to the agent (`/api/incident`) and to the NudgeBee Event Webhook.
2. **Gather.** The agent collects guardian state, pods (phase, restarts, OOMKilled), pod CPU/memory from
   `metrics.k8s.io`, DB pool metrics, recent ERROR/Traceback log lines, RDS status/class, NudgeBee's
   `ai_execute_investigation` RCA and the newest readiness report.
3. **Plan.** Claude (`claude-sonnet-5-5` via the Anthropic API; provider/model from Flagsmith `agent_config`)
   returns a plan through a forced tool call: plain founder summary, technical summary and ≤3 options
   (recommended / alternative / do nothing), each with impact and cost in rupees. Pydantic validates it; one retry,
   then the rule-based planner. Flag `ai_agent` OFF = notify only.
4. **Ask.** ops-bot turns the summary into a Tanglish text + Bulbul voice note with reply buttons (`opt:<id>:<key>`).
   Sarvam pipeline: Saaras v4 STT (codemix, auto language) → sarvam-105b intent (structured) →
   sarvam-105b-conversations reply → Mayura code-mixed roman (Tanglish) or Sarvam transliterate (Tamil script) →
   Bulbul v4-flash TTS at the `ops_voice` pace.
5. **Act.** On a tap or "yes", ops-bot posts the decision; the agent runs that option's actions in order, each
   validated against `insidious.yaml` → `agent.guardrails`, and posts every step to the cockpit feed.
6. **Recover.** guardian sees three healthy samples and resolves the incident (NudgeBee gets `status: resolved`).

Verified live: guardian fired ~30 s into a surge (checkout p95 4.8 s, pool 5/5 busy); Claude proposed
`heavy_features` OFF + scale store-api to 2 + `db_pool` 10 ("About ₹4 more per hour"); executed after the founder's
tap. Earlier on the same cluster, shedding `heavy_features` took p95 from 4.8 s to 180 ms at 80 rps with 0 errors.

### Flagsmith as the control plane

Every runtime lever that isn't Kubernetes or RDS is a Flagsmith flag or remote config, so the agent, the cockpit,
the CLI and a human in the Flagsmith dashboard all pull the same levers:

| Lever | Flag / config | Reader |
|---|---|---|
| Shed heavy features | `heavy_features` → `recommendations`, `hd_images`, `live_reviews` (dependencies) | store SDK |
| Keep big carts whole | segment `high_value_cart` (trait `cart_value` > 2000) | store SDK (identity `fk_sid`) |
| Checkout experiment | `checkout_flow` multivariate full/lite | store |
| DB pool size | `db_pool` JSON | store `insidious.db`, rebuilt ≤5 s |
| Read replica | `read_from_replica` + `db_read_url` | store `insidious.db` |
| Payment failover | `payment_gateway` | store |
| Founder voice | `ops_voice` JSON on identity `founder:<phone>` | ops-bot |
| AI behaviour / kill switch | `agent_config` JSON, `ai_agent` | agent (cannot change these two itself) |

Reads are local evaluation (5 s refresh); writes go through the Admin API (CLI `init`/`rehearse`, `/ops` cockpit
with passcode, agent after approval).

### Security choices

- Nothing changes without the founder's approval; autopilot is off in `agent_config`.
- Executors accept only the action catalog in CONTRACTS.md and reject params outside the guardrails.
- The AWS role is scoped to `insidious-*` RDS instances; k8s RBAC limits the agent to namespace `store`.
- Internal POSTs carry `X-Insidious-Token`; cockpit flag writes need `INSIDIOUS_OPS_PASSCODE`; Meta and Flagsmith
  webhooks are HMAC-verified.
- The EC2 security group allows 443 only from Cloudflare.
- `insidious scan` skips `.env*`, keys and certificates by name (never opened) and redacts secret-looking lines.

### Not done yet

- NudgeBee workflows: build guides in `nudgebee/workflows/`, not built in the NudgeBee UI.
- RDS replica / instance-class actions are implemented and guarded but weren't exercised in the recorded run.
- Single node, so scaling adds replicas, not machines.
