# NudgeBee setup for Insidious (do this once, before building the workflows)

Order: agent on kind → Event Webhook → Secrets → (optional) Workflow Webhook → build the three workflows
([sale-incident-guard](sale-incident-guard.md), [pre-sale-warmup](pre-sale-warmup.md),
[post-sale-report](post-sale-report.md)) → copy the workflow IDs into `.env`.

Legend used in every guide: **[verified]** = from NudgeBee docs; **[nbctl]** = from NudgeBee's `nbctl` CLI
source, not in public docs; **[UNVERIFIED]** = our best guess. Check it on the first test run and fix the guide.

---

## 1. Install the NudgeBee agent on kind (only on kind)

1. NudgeBee UI → **Admin → Integrations → Kubernetes Clusters → Add K8s Account**. Name it `insidious-kind`,
   mark it **Non-production**, copy the agent auth key. [verified]
2. On your laptop (cluster already up with `make kind-up`):
   ```bash
   make nb-agent-install NB_AGENT_KEY=<agent-key>
   ```
   This downloads NudgeBee's `installation.sh` and runs it with `-a <key> -d true` (`-d true` turns off the eBPF
   node agent, which can't attach on kind) [verified]. The script uses kubectl/helm against the *current*
   context, so our wrapper runs it with `KUBECONFIG` pointing at a file that contains **only** the
   `kind-insidious` context. It can't touch any other cluster.
3. Check: `KUBECONFIG=~/.kube/kind-insidious.kubeconfig kubectl get pods -n nudgebee-agent`. Inventory shows up in
   NudgeBee in about 5 min; rightsizing and cost need about 1 h of history [verified]. Install early.
4. Copy the **account ID** of this cluster (NudgeBee UI, account settings or URL) into `.env` as
   `NUDGEBEE_ACCOUNT_ID`.

## 2. Event Webhook (guardian → NudgeBee events)

1. **Admin → Integrations → Webhooks → Event Webhook** → create one named `insidious-guardian`, scoped to the
   `insidious-kind` account. [verified]
2. Copy the URL. It ends in `/api/webhooks/event?token=…`. Put the **full URL** in `.env` as
   `NUDGEBEE_EVENT_WEBHOOK_URL`. guardian also sends the token as `Authorization: Bearer <token>`, which NudgeBee
   recommends [verified].
3. Payload (what guardian sends, also in [sample-event.json](sample-event.json)): required `id`, `fingerprint`,
   `status` (`firing|resolved`), `title`, `rule_name`. Optional `severity`, `labels`, `subject {name,type,namespace}`,
   `description`. 200 = OK, 400 = validation errors listed [verified].
4. Test it without guardian:
   ```bash
   make nb-test-event              # prints the request
   make nb-test-event CONFIRM=1    # sends a firing event with a fresh id
   make nb-test-event STATUS=resolved CONFIRM=1
   ```
   The event should appear under Events with an AI investigation (RCA) starting automatically for
   `severity: high` [verified: high-priority events are analysed by default]. **Time the RCA three times**
   (open question 6). That is how long the demo waits between "p95 red" and "WhatsApp approval".

## 3. Secrets used by the workflows

Create these in NudgeBee Secrets [UNVERIFIED: location. Look under Admin → Secrets, or the Secrets panel in the
Workflow Builder]. Templates read them as `{{ Secrets.<name> }}` [verified syntax].

| Secret name | Value | Used by |
|---|---|---|
| `flagsmith_org_key` | Flagsmith **organisation** API key (`FLAGSMITH_ORG_API_KEY`) | HTTP tasks → Flagsmith Admin API (`Authorization: Api-Key …`) |
| `festivekit_token` | same as `INSIDIOUS_INTERNAL_TOKEN` in `.env` | HTTP tasks → ops-bot (`X-Insidious-Token`) |

Values you paste as literals in the guides (or as Configs, `{{ Configs.x }}`, if your tenant has them [UNVERIFIED]):

| Placeholder in guides | Value | Where from |
|---|---|---|
| `<OPS_BOT>` | `https://<your-ngrok-domain>` (= `PUBLIC_BASE_URL`) | ngrok static domain; ingress routes `/nb/*` to ops-bot |
| `<FS_ADMIN>` | `https://api.flagsmith.com/api/v1` | `FLAGSMITH_ADMIN_URL` |
| `<FS_ENV_KEY>` | Flagsmith environment key | `FLAGSMITH_ENVIRONMENT_KEY` |
| `<FS_ID_heavy_features>`, `<FS_ID_payment_gateway>` | environment-level featurestate ids | `make fs-ids` prints them |
| `<ACCOUNT_ID>` | NudgeBee account id of the kind cluster | `NUDGEBEE_ACCOUNT_ID` |

Why static featurestate ids: the environment-level featurestate id for a flag never changes while Feature
Versioning v2 is off, and it avoids indexing into JSON arrays in templates (syntax unverified). Each guide also
shows the dynamic lookup as a fallback.

**Do not enable Flagsmith Feature Versioning v2.** It is irreversible and breaks the featurestate endpoints.

## 4. (Optional) Workflow Webhook, Plan B trigger

Use it only if the Event trigger on `investigation.completed` is unreliable on demo day.

1. **Admin → Integrations → Webhooks → Automation Webhook → "Add Workflow Webhook Account"**: name
   `insidious-direct`, select the account(s). The integrations page shows **no URL**; that's expected [verified].
2. In the workflow editor add a **Webhook** trigger and pick `insidious-direct` in **Webhook Integration**. The URL
   appears in the trigger config with a copy icon, ending in `/api/webhooks/workflow?token=…` [verified]. The token
   query param authenticates; no header needed [verified].
3. Optional filter expression (≤500 chars, must render exactly `true`):
   `{{ webhook_payload.status == "firing" }}` [verified pattern; field path UNVERIFIED].
4. Tasks read the body as `{{ Inputs.webhook_json.<field> }}` (parsed) or `{{ Inputs.webhook_payload }}` (raw)
   [verified]. With this trigger there is no NudgeBee event, so swap `llm.event_investigate` for `llm.investigate`
   with the payload as the message (see the Plan B note in sale-incident-guard.md).
5. To use it, point guardian at it by setting `NUDGEBEE_EVENT_WEBHOOK_URL` to this URL. guardian's payload is plain
   JSON, so the workflow receives it unchanged.

## 5. Timeouts (every workflow)

Workflow **Settings → Automation Timeout = 30m**, and on each `core.approval` task **Task Timeout = 30m**. The
default Automation Timeout is 5 min, and it caps the whole run including the approval wait. If nobody answers
in time the step fails [verified].

## 6. After building

Copy each workflow's ID from its URL in the Workflow Builder into `.env`: `NUDGEBEE_WF_INCIDENT_ID`,
`NUDGEBEE_WF_PRESALE_ID`, `NUDGEBEE_WF_POSTSALE_ID`. Then `make secrets restart` (kind) so ops-bot and guardian
pick them up. Export each workflow's JSON (if the builder offers export) and a screenshot into this folder:
`sale-incident-guard.json` / `.png`, and so on.

## Unverified details to confirm on the first run (fix the guides when you know)

| # | Detail | How to check | Fallback already built in |
|---|---|---|---|
| U1 | Template for the current execution id: `{{ Inputs.workflow_execution_id }}` (listed as an automatic input [verified]); whether it equals the `execution_id` that `workflow_complete_approval` wants | Look at the run's Inputs panel; compare with `workflow_get_execution` | ops-bot polls `workflow_list_executions` + `workflow_get_execution` for the waiting `core.approval` task if ids are empty |
| U2 | `task_id` for `workflow_complete_approval`: the task's id in the definition (`approve`) or a runtime id | Read `tasks[].id` in `workflow_get_execution` while it waits | same polling fallback |
| U3 | Custom `approval_options` through the API (status other than approved/rejected) | Try it once | We keep approve/reject; the action is chosen earlier by `llm.classify` |
| U4 | `core.approval` output `status` value: `approved` vs `approve` | Read the task output after one run | The switch below matches both |
| U5 | Event-trigger data paths: `{{ Inputs.event_id }}`, `{{ Inputs.event.title }}`, `{{ Inputs.event.labels.p95_ms }}`, `{{ Inputs.account_id }}` | Run with `make nb-test-event CONFIRM=1`, open the execution's Inputs JSON | Use literals or the `llm.event_investigate` output |
| U6 | JSON-escaping of templated text inside an HTTP body (quotes/newlines in RCA text) | Look for a `tojson`/`json` filter | We pass text through `llm.summary` asking for one line without double quotes |
| U7 | Array indexing in templates (`...body.results[0].id`) | Try in a `core.print` task | Static featurestate ids |
| U8 | Event trigger filter field for our rule: `rule_name` → aggregation key / alert name | Inspect the created event | Filter on title contains "SLO" or the advanced expression |
| U9 | Whether `integrations.http` runs from NudgeBee cloud or the in-cluster agent | Watch ingress logs (`make logs SVC=ops-bot`) | ngrok URL works either way |
| U10 | `k8s.horizontal_rightsize` `scaling_mode` value names | Read the dropdown | Pick the "set to an exact count" option, `change_to: 3` |
