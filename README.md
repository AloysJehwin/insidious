# Insidious

**An on-call SRE for small D2C brands that lives in the founder's WhatsApp.** It talks Tanglish, explains incidents
in plain words, and fixes things with Flagsmith flags, Kubernetes and the database only after the founder says yes.

- **Demo video (1:51):** https://youtu.be/-w22mQeU36U  ·  also in the repo: [`video/out/insidious-demo-2min.mp4`](video/out/insidious-demo-2min.mp4)
- **Live store:** https://festive.aloysjehwin.com · **Live ops cockpit:** https://festive.aloysjehwin.com/ops
  (read-only; flag toggles need a passcode)
- **Repo:** `<REPO URL: add before submitting>` · Demo shot list: [docs/DEMO.md](docs/DEMO.md) · Design:
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md)
- Built at WeMakeDevs Festive Hacks, 25 Nov 2026.

## The problem

Mid-size D2C brands that run their own stack crash on festive sale nights, the one night that makes their quarter.
They are too big for a hosted storefront and too small for an SRE team, so the person on call is a non-technical
founder.

> **Ravi** runs **Jeffi**, a D2C brand on its own self-hosted store. Last festive sale, checkout slowed to a crawl
> at 8 PM. Ravi can't read Grafana or kubectl, but he answers a WhatsApp voice note in seconds, in Tanglish (Tamil
> mixed with English). Insidious works where he already is: it sends him a voice note that says what is wrong,
> what it wants to do and what it costs, with buttons. He taps one, and the store recovers.

## How it works

```
                 Ravi's phone (WhatsApp: Tanglish voice notes, reply buttons, "yes"/"Tamil"/"English")
                        ▲  voice note + buttons                │ voice note / tap / text
                        │            Meta WhatsApp Cloud API   ▼
 ┌───────────── k3s on AWS EC2 (t3.xlarge, Mumbai), behind Cloudflare → Traefik ──────────────────────────┐
 │  ops-bot ◄──── Sarvam AI ────► Saaras v4 STT · sarvam-105b intent · Mayura Tanglish · Bulbul v4 TTS   │
 │    │ ▲  plan + options / founder's choice                                                             │
 │    ▼ │                                                                                                │
 │  agent (Claude Sonnet) ── telemetry: pods/CPU/memory, DB pool, error logs, RDS, NudgeBee RCA ──┐      │
 │    │  after approval, whitelisted actions only                                                 │      │
 │    ├──► Flagsmith Admin API: heavy_features off, db_pool, read_from_replica, payment_gateway    │      │
 │    ├──► Kubernetes: scale store-api, set CPU/memory                                            │      │
 │    ├──► AWS RDS: read replica / instance class (insidious-* only, scoped IAM role)            │      │
 │    └──► NudgeBee workflow · notify founder                                                     │      │
 │                                                                                                │      │
 │  guardian: checkout p95, error rate, DB pool every 5 s ── incident ──► agent + NudgeBee Event Webhook │
 │  store-api "Jeffi" (FastAPI + insidious SDK, Flagsmith local eval) ──► AWS RDS PostgreSQL           │
 │  NudgeBee agent (Prometheus, Loki, ClickHouse, node agent) ──► NudgeBee AI root-cause investigation  │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**The hero loop (verified live on the cluster):** a traffic surge pushes checkout p95 to 4.8 s with the DB pool 5/5
busy → guardian fires about 30 s in → the Claude agent gathers telemetry (including NudgeBee's RCA) and writes a plan
→ Ravi gets a Tanglish voice note plus text: *"Checkout is very slow right now (about 5 seconds instead of under 1)
... I recommend pausing product suggestions, adding one more server and giving the database more room. About ₹4
more per hour."* → he taps the button → the agent turns `heavy_features` OFF in Flagsmith, scales store-api to 2
and sets `db_pool` to 10 → p95 goes green on `/ops` → guardian resolves the incident on its own. In an earlier run
on the same cluster, shedding `heavy_features` alone took checkout p95 from **4.8 s to 180 ms at 80 rps with 0
errors**.

## Sponsor tools: what breaks if you remove each

| Tool | What it does in Insidious | Remove it and... |
|---|---|---|
| **Flagsmith** | The runtime control plane. 13 flags: feature shedding (parent flag + dependencies), DB pool size, replica reads, payment gateway, the founder's voice, the Claude agent's model and kill switch. Two of the three actions in the verified fix were Flagsmith writes. | Every fix becomes a redeploy or "add more servers". You can't shed load, resize the pool live, move reads to a replica or switch off the AI. |
| **NudgeBee** | Observability and incident record for the cluster: agent on k3s (Prometheus, Loki, ClickHouse, node agent), SLO incidents via the Event Webhook, AI root-cause investigation fed into Claude's evidence, AWS cost/inventory via CloudFormation. | The agent plans from guardian's numbers alone, with no root cause, no logs/metrics store and no incident trail outside our own code. |
| **Sarvam AI** | The founder's interface: voice notes in, intent out, Tanglish/Tamil/Hindi/English replies and voice notes back. | Ravi gets English alerts on a dashboard he can't read at 8 PM, and can't approve a fix by voice. |
| Claude (Anthropic API) | Turns telemetry into a plain-language plan with up to 3 options and a cost, then runs only the approved one. | Someone technical has to read the metrics and decide. |

## Flagsmith: every feature used, and where

`insidious init` creates all of this idempotently from [flagsmith/flags.yaml](flagsmith/flags.yaml) through the
Admin API ([cli/insidious_cli/flagsmith_admin.py](cli/insidious_cli/flagsmith_admin.py)).

| Flagsmith feature | How Insidious uses it | Where |
|---|---|---|
| **Boolean flags + flag dependencies** | `heavy_features` is the parent of `recommendations`, `hd_images`, `live_reviews`. One flip sheds all three; `@fk.degradable()` serves cached fallbacks. The SDK also enforces the parent itself. | `flags.yaml`, `sdk/insidious/degrade.py`, `services/store` |
| **Segments + identity traits** | Every shopper is an identity (`fk_sid` cookie) with trait `cart_value`. Segment `high_value_cart` (`cart_value > 2000`) overrides `heavy_features` back ON, so big carts keep the full experience while everyone else is shed. | `services/store/app/main.py` |
| **Multivariate A/B** | `checkout_flow` = `full` / `lite` at 50/50, counted by `insidious_orders_total{checkout_flow}`. Lite skips recommendations at checkout. | store checkout |
| **Remote config** | `db_pool` (JSON): the store rebuilds its SQLAlchemy pool live (checked every ≤5 s). `db_read_url` + flag `read_from_replica`: catalogue reads move to an RDS replica, writes stay on the primary. `payment_gateway`: razorpay ↔ cashfree failover. | `sdk/insidious/db.py`, `services/store/app/db.py` |
| **Identity-level remote config** | `ops_voice` on identity `founder:<phone>`: language, style, speaker and pace of Ravi's voice notes. Live now: `ta-IN`, `tanglish`, pace 1.25. | `services/ops-bot/app/voiceconf.py` |
| **Remote config for AI behaviour** | `agent_config`: Claude provider, model, autopilot (off), max actions per plan. | `services/agent/app/flags.py` |
| **Kill switch** | `ai_agent` OFF → the agent only notifies the founder and never proposes or runs plans. The agent can't change `ai_agent` or `agent_config` itself (protected flags). | `services/agent/app/config.py` |
| **Local evaluation** | The store evaluates flags in-process from the environment document (5 s refresh), so load tests and sale traffic never burn API quota. Offline mode falls back to `flags.yaml` defaults. | `sdk/insidious/flags.py` |
| **Admin API writes** | (1) `insidious init` creates flags, variants, segments, overrides and dependencies. (2) The `/ops` cockpit toggles flags (passcode-protected). (3) The Claude agent flips flags and sets values after the founder approves. (4) `insidious rehearse` flips `heavy_features` between passes. | CLI, `services/store/app/ops_flags.py`, `services/agent/app/flagsmith_admin.py` |
| **Environment webhook** | ops-bot has an HMAC-verified `/flagsmith/webhook` that posts each change to the cockpit feed and texts the founder. | `services/ops-bot/app/main.py` |
| **Flags as code, generated** | `insidious scan` reads any repo and writes a `flags.yaml` for it (27 flags for the real Jeffi monorepo); `--apply` creates them. | `cli/insidious_cli/scan.py`, [examples/jeffi-scan](examples/jeffi-scan) |

**Flag-aware load testing.** `insidious rehearse` runs the same ramp twice, once with `heavy_features` on and once
with it off, so the flag gets a price tag: "your store breaks at X rps; shedding heavy features raises that by Y%".
The readiness report feeds guardian's incident labels and the agent's plan.

## NudgeBee

- **Agent on the k3s cluster:** Prometheus, Loki, ClickHouse and the node agent.
- **Event Webhook:** guardian posts every SLO incident (firing and resolved, same id and fingerprint) to NudgeBee.
- **AI root cause in the plan:** on each incident the Claude agent calls NudgeBee's `ai_execute_investigation` and
  includes the RCA in its telemetry (≤60 s, skipped on error).
- **AWS account connected** via CloudFormation for cost and inventory.
- **Workflows:** build guides included in [nudgebee/workflows/](nudgebee/workflows/00-setup.md)
  (`sale-incident-guard`, `pre-sale-warmup`, `post-sale-report`). The agent can run a NudgeBee workflow as one of its
  whitelisted actions.

## Sarvam AI

The WhatsApp ops line (Meta Cloud API test number) for the non-technical founder, in `services/ops-bot`:

1. **Saaras v4** speech-to-text, `codemix` mode with automatic language detection. Verified on a Tamil voice note:
   `ta-IN`, probability 1.0.
2. **sarvam-105b** parses intent with structured output (approve, reject, status, why, language switch...).
3. **sarvam-105b-conversations** writes the replies.
4. **Mayura** (`code-mixed`, roman output) produces Tanglish; **Sarvam transliterate** turns romanised Tamil into
   Tamil script when Ravi asks for "Tamil".
5. **Bulbul v4-flash** speaks the reply as a WhatsApp voice note, at the pace set in Flagsmith `ops_voice`.

Default reply style is Tanglish. Ravi switches by saying "Tamil", "English", "Hindi" or "Tanglish".

## Claude ops agent

`services/agent` (Anthropic API, `claude-sonnet-5-5`, model set by Flagsmith `agent_config`):

- **Collects** guardian state, pods and their CPU/memory, DB pool metrics, recent error logs, RDS status, the NudgeBee
  RCA and the newest readiness report.
- **Plans** with tool-use structured output (pydantic-validated, one retry, then a rule-based fallback): a plain
  founder summary and up to 3 options (recommended, alternative, do nothing), each with impact and cost in rupees.
- **Executes only after approval** (button tap or "yes"). Autopilot is off.
- **Whitelisted actions with guardrails** from `insidious.yaml`: Flagsmith flag/value, k8s scale (≤5 replicas,
  namespace `store` only), k8s resources (≤2 CPU, ≤2Gi), DB pool, RDS read replica or instance class (IAM role
  `festivekit-agent` on the EC2 instance profile, scoped to `festivekit-*` DBs, t4g micro/small/medium), NudgeBee
  workflow, notify. Anything else is rejected.

## Quickstart

### A. Docker Compose, all mocks (no accounts)

```bash
make compose-up                 # store, payments-mock, guardian, ops-bot, prometheus (SARVAM_MOCK/NUDGEBEE_DRY_RUN/WHATSAPP_DRY_RUN=1)
open http://localhost:8000/ops  # cockpit
make venv                       # .venv with the SDK + CLI
make init                       # green checklist; Flagsmith/NudgeBee steps report offline/dry-run
make quick-rehearse             # two-pass flag-aware load test -> reports/readiness-*.md
make guard && make festive-rush # watch /ops go red (or: make compose-rush, k6 in a container)
make simulate-approval          # ops-bot logs the WhatsApp voice note + buttons it would send
make reset
```
Without a Flagsmith key the SDK uses `flagsmith/flags.yaml` defaults and the store uses SQLite. The Claude agent
isn't in the compose file; `AGENT_MOCK=1` gives it a deterministic rule-based planner for local runs and tests.

### B. Real mode: k3s on EC2 (what runs at festive.aloysjehwin.com)

1. EC2 t3.xlarge in ap-south-1 with k3s (Traefik ingress). Security group allows 443 only from Cloudflare; DNS is
   Cloudflare-proxied.
2. RDS PostgreSQL `festivekit-primary` (db.t4g.micro). Instance profile includes the scoped IAM role
   `festivekit-agent`.
3. Fill `.env` from [.env.example](.env.example) (Flagsmith, Sarvam, NudgeBee, WhatsApp, Anthropic, `DATABASE_URL`,
   `DATABASE_RDS_ID`, `INSIDIOUS_OPS_PASSCODE`). Never commit it.
4. Build the images (`make build`) and make them available to k3s, then deploy with an explicit kubeconfig:
   ```bash
   make config-sync
   KUBECONFIG=~/.kube/festivekit-k3s.yaml make secrets KUBE_CONTEXT=festivekit-k3s ALLOW_NON_KIND=1
   kubectl --kubeconfig ~/.kube/festivekit-k3s.yaml --context festivekit-k3s apply -k deploy/k3s
   ```
5. Install the NudgeBee agent (NudgeBee UI → Add K8s Account) and set `NUDGEBEE_EVENT_WEBHOOK_URL`.
6. Point the Meta webhook at `https://<your-domain>/whatsapp/webhook`; the founder messages the test number once.
7. `insidious init`, then `insidious guard`.

kind on a laptop also works (`make up`, see the Makefile).

## The commands

```bash
insidious init        # flags, variants, segments, overrides, dependencies in Flagsmith; k8s apply; NudgeBee checks
insidious rehearse    # flag-aware load test (heavy_features on vs off) -> readiness report + voice brief
insidious guard       # sale-day mode: arm guardian (+ NudgeBee pre-sale workflow when configured)
insidious scan <repo> # Claude reads an existing codebase and writes insidious.yaml, flags.yaml, scan-report.md
```
`insidious scan` never opens `.env*`, keys or certificates and redacts secret-looking lines before analysis. A
mock-mode run (heuristic analyser, no network) on the real Jeffi Stores monorepo found 43 env toggles, 310 routes,
8 services and Postgres + Redis, and proposed 27 Flagsmith flags, 8 of them under `heavy_features`. See
[examples/jeffi-scan](examples/jeffi-scan/docs/scan-report.md).

**SDK:** `pip install insidious` (publishing today). Python, MIT; builds and passes `twine check`. See
[sdk/README.md](sdk/README.md).

## Repo layout

```
sdk/insidious/        SDK: Flagsmith local eval, @degradable, Prometheus middleware, insidious.db (pool, replica)
cli/                   Typer CLI: init, rehearse, guard, status, reset, scan
services/store         Jeffi store + /ops cockpit (flag toggles via Admin API)
services/agent         Claude ops agent: telemetry, plans, guarded executors (Flagsmith, k8s, RDS, NudgeBee)
services/guardian      SLO + DB pool watcher -> agent + NudgeBee Event Webhook; /api/state
services/ops-bot       WhatsApp + Sarvam bridge: voice in, Tanglish voice notes and buttons out
services/payments-mock razorpay/cashfree mocks
deploy/k8s, deploy/k3s Kustomize manifests (k3s overlay: Traefik, tuned CPU cost)
flagsmith/flags.yaml   desired Flagsmith state (13 flags, 1 segment)
nudgebee/workflows/    NudgeBee workflow build guides + sample event
examples/jeffi-scan/   insidious scan output for the real Jeffi monorepo
loadtest/              k6: rehearse.js, festive-rush.js
```

## Honest limitations

- **Payments are mocked** (razorpay/cashfree mocks). No real money moves.
- **Meta test number:** at most 5 recipients, and the founder must message it within 24 hours.
- **NudgeBee workflows:** the build guides are included but the workflows aren't built in the NudgeBee UI yet. The
  live loop runs through the Claude agent; NudgeBee provides observability, RCA and the incident record.
- **Single-node k3s:** "one more server" in the founder message means one more store-api replica on the same node.
- **Verified vs implemented:** the live run used Flagsmith flag + value, k8s scale and the DB pool. The RDS
  replica/instance-class actions are implemented and guarded but weren't part of the recorded run.
- **Jeffi scan** in `examples/` ran in mock (heuristic) mode, not with Claude.
- NudgeBee GraphQL shapes come from NudgeBee's `nbctl` CLI source, not public docs. Flagsmith writes use the legacy
  featurestate endpoints, so Feature Versioning v2 must stay off.

## AI assistance disclosure

The code, manifests and docs were written with **Claude Code** (Anthropic's AI coding assistant) under the team's
direction. We chose the problem, designed the product, architecture and contracts, deployed and ran it, and
reviewed and tested the output.
