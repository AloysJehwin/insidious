# Workflow: `sale-incident-guard` (the hero loop)

guardian posts an SLO event → NudgeBee runs its AI RCA → this workflow picks a remedy, asks Ravi on WhatsApp
(through ops-bot), waits for approval, then flips Flagsmith or scales store-api, and tells Ravi it's done.

Prerequisites: [00-setup.md](00-setup.md) steps 1–3 done (agent, Event Webhook, Secrets), ngrok up
(`make ngrok`), and `make fs-ids` output at hand. Placeholders `<OPS_BOT>`, `<FS_ADMIN>`, `<FS_ENV_KEY>`,
`<FS_ID_heavy_features>`, `<FS_ID_payment_gateway>`, `<ACCOUNT_ID>` are defined in 00-setup.md §3.
Tags: [verified] docs, [nbctl] nbctl source, [UNVERIFIED] check on first run.

```
trigger: Event, phase investigation.completed, rule insidious-slo
  rca (llm.event_investigate) → brief (llm.summary) → decide (llm.classify)
  → ask_founder (integrations.http → ops-bot /nb/approval-request)
  → approve (core.approval, 30 min)
  → gate (core.switch on approval status)
       approved → act (core.switch on decide.selected_branch)
            shed_heavy_features → fs_shed (HTTP PATCH Flagsmith) → scale_after_shed (k8s.horizontal_rightsize, optional)
            scale_out           → scale (k8s.horizontal_rightsize → 3)
            failover_payment    → fs_failover (HTTP PATCH payment_gateway=cashfree)
            ignore              → notify_ignored
       default  → notify_rejected
  → wait_recovery (core.wait 45s) → notify_done (HTTP → ops-bot /nb/notify)
```

---

## Workflow settings

- Name: `sale-incident-guard`. Description: "Insidious: RCA → founder approval on WhatsApp → shed load / scale".
- **Automation Timeout: `30m`** (default 5 min kills the approval wait) [verified].
- Copy the workflow ID from the URL into `.env` → `NUDGEBEE_WF_INCIDENT_ID`.

## Trigger

- Type: **Event** [verified].
- Phase: **`investigation.completed`** (runs after NudgeBee's RCA is done) [verified].
- Accounts: `insidious-kind`.
- Filter (pick whichever the UI offers):
  - Event type / aggregation key = `insidious-slo` [UNVERIFIED U8: Event Webhook `rule_name` is assumed to become the
    alert name / aggregation key], or
  - Advanced expression: `{{ event.rule_name == "insidious-slo" and event.status == "firing" }}` [UNVERIFIED field names].
  - Namespace = `store` is a safe extra filter if offered.
- First run: `make nb-test-event CONFIRM=1`, open the execution, and copy the trigger's Inputs JSON into a note.
  Fix the `Inputs.event...` paths below if they differ (U5).

## Tasks, in order

### 1. `rca`: AI task **`llm.event_investigate`** [verified]
| Field | Value |
|---|---|
| id | `rca` |
| account_id | `<ACCOUNT_ID>` (or `{{ Inputs.account_id }}` [UNVERIFIED]) |
| event_id | `{{ Inputs.event_id }}` [UNVERIFIED U5; alternative `{{ Inputs.event.id }}`] |
| model | empty (account default) |
Outputs: `investigation` (the RCA text), `status` COMPLETED/FAILED [verified].
If the trigger Inputs already contain the RCA text, you can delete this task and reference that path instead.

### 2. `brief`: AI task **`llm.summary`** [verified]
Makes the RCA safe to embed in a JSON string (one line, no double quotes; U6).
| Field | Value |
|---|---|
| id | `brief` |
| message | `Summarise this incident for a non-technical shop owner in ONE line under 280 characters. Use no double quotes, no newlines, no markdown. Include the p95 latency and requests per second. Incident: {{ Inputs.event.title }}. RCA: {{ Tasks['rca'].output.investigation }}` |
Output: `{{ Tasks['brief'].output.data }}`.

### 3. `decide`: AI task **`llm.classify`** [verified]
| Field | Value |
|---|---|
| id | `decide` |
| prompt | see below |
| options | 4 rows below |

Prompt:
```
You are the on-call SRE for Jeffy, a D2C store on festive sale night. Pick ONE remedy for this SLO breach.
Event: {{ Inputs.event.title }}
Labels: p95_ms={{ Inputs.event.labels.p95_ms }}, rps={{ Inputs.event.labels.rps }}, heavy_features={{ Inputs.event.labels.heavy_features }}, replicas={{ Inputs.event.labels.replicas }}, capacity_gain_if_shed={{ Inputs.event.labels.capacity_gain_if_shed }}
RCA: {{ Tasks['rca'].output.investigation }}
Rehearsal data: shedding heavy_features (recommendations, HD images, live reviews) raised the checkout breaking point by the capacity_gain_if_shed above. Prefer shedding over scaling when heavy_features is on; big carts keep the full experience via a Flagsmith segment.
```
Options (name / description):
| name | description |
|---|---|
| `shed_heavy_features` | Latency is high because store-api is CPU-bound and heavy_features is still on. Turn off heavy features in Flagsmith. Default choice for a latency breach under high traffic. |
| `scale_out` | Pods are at their CPU limit but heavy_features is already off (or shedding is not enough). Scale store-api from 1 to 3 replicas. |
| `failover_payment` | Errors or latency come from the payment gateway (razorpay mock), not from store CPU. Switch payment_gateway to cashfree. |
| `ignore` | Already recovered, a false alarm, or not caused by traffic. Do nothing. |
Output: `{{ Tasks['decide'].output.selected_branch }}` [verified].

### 4. `ask_founder`: Integration task **`integrations.http`** [verified]
| Field | Value |
|---|---|
| id | `ask_founder` |
| method | `POST` |
| url | `<OPS_BOT>/nb/approval-request` |
| auth_type | `none` |
| headers | `Content-Type: application/json` and `X-Insidious-Token: {{ Secrets.festivekit_token }}` |
| timeout | `30s` (default) |
| body | below |
```json
{
  "execution_id": "{{ Inputs.workflow_execution_id }}",
  "task_id": "approve",
  "workflow_id": "{{ Inputs.workflow_id }}",
  "action": "{{ Tasks['decide'].output.selected_branch }}",
  "summary_en": "{{ Tasks['brief'].output.data }}",
  "numbers": {
    "p95_ms": "{{ Inputs.event.labels.p95_ms }}",
    "rps": "{{ Inputs.event.labels.rps }}",
    "gain": "{{ Inputs.event.labels.capacity_gain_if_shed }}",
    "replicas_to": 3
  }
}
```
- `execution_id` / `task_id`: [UNVERIFIED U1/U2]. If they render empty or wrong, ops-bot finds the waiting
  `core.approval` itself (polls `workflow_list_executions` for `NUDGEBEE_WF_INCIDENT_ID`, then
  `workflow_get_execution`) [nbctl shapes]. Leaving them as `""` is fine.
- Expect `{"ok": true, "pending_id": "..."}` (status 200). A 401 means the token secret doesn't match `.env`.

### 5. `approve`: Core task **`core.approval`** [verified]
| Field | Value |
|---|---|
| id | `approve` (keep exactly this; ops-bot sends it as `task_id`) |
| message | `Insidious wants to {{ Tasks['decide'].output.selected_branch }} on store-api. {{ Tasks['brief'].output.data }}` |
| approval_type | `in_app` (default; no notification, ops-bot is the notifier) [verified] |
| approval_options | leave default `["approve","reject"]` (custom options UNVERIFIED U3) |
| **Task Timeout** | **`30m`** |
Outputs: `status`, `approver`, `comments` [verified]. ops-bot completes it with
`workflow_complete_approval(request: {execution_id, task_id, status: "approved"|"rejected", comments: "voice: haan karo (hi-IN, 0.97)"})` [nbctl].
Anyone can also click Approve in the NudgeBee execution view. That is the manual fallback on stage.

### 6. `gate`: Core task **`core.switch`** [verified]
| Field | Value |
|---|---|
| id | `gate` |
| expression | `{{ Tasks['approve'].output.status }}` |
| case `approved` → next | `act` |
| case `approve` → next | `act` (U4: covers either spelling) |
| default → next | `notify_rejected` |

### 7. `act`: Core task **`core.switch`** [verified]
| Field | Value |
|---|---|
| id | `act` |
| expression | `{{ Tasks['decide'].output.selected_branch }}` |
| case `shed_heavy_features` → next | `fs_shed` |
| case `scale_out` → next | `scale` |
| case `failover_payment` → next | `fs_failover` |
| default (`ignore`) → next | `notify_ignored` |

### 8a. `fs_shed`: **`integrations.http`**, Flagsmith Admin API: heavy_features OFF
| Field | Value |
|---|---|
| id | `fs_shed` |
| method | `PATCH` |
| url | `<FS_ADMIN>/environments/<FS_ENV_KEY>/featurestates/<FS_ID_heavy_features>/` (keep the trailing slash) |
| headers | `Authorization: Api-Key {{ Secrets.flagsmith_org_key }}`, `Content-Type: application/json` |
| body | `{"enabled": false}` |
Expect 200 with the updated featurestate. The `high_value_cart` segment override keeps heavy features ON for carts
over ₹2000, and nothing in the workflow touches it. The store's local-eval SDK sees the change within 5 s, and the
Flagsmith environment webhook tells ops-bot, which updates the cockpit feed and texts Ravi.
Endpoint shape per CONTRACTS.md: legacy featurestate API, only while Feature Versioning v2 is off
[UNVERIFIED against Swagger; prove it first with `python3 scripts/flagsmith-toggle.py --dry-run set heavy_features=off`,
then without `--dry-run`].

Dynamic alternative (no hard-coded id; needs array indexing U7):
1. `fs_find`: `GET <FS_ADMIN>/environments/<FS_ENV_KEY>/featurestates/?feature_name=heavy_features`, same auth header.
2. In `fs_shed`, url `.../featurestates/{{ Tasks['fs_find'].output.body.results[0].id }}/`. The environment-level row
   is the one with `identity: null` and `feature_segment: null`; check it's `results[0]`.

### 8b. `scale_after_shed` (optional, makes "pods 1 → 3" visible in the video): **`k8s.horizontal_rightsize`** [verified]
Same fields as 9 below; next → `wait_recovery`. Skip it if you want the flag flip alone to carry the recovery.

### 9. `scale`: Kubernetes task **`k8s.horizontal_rightsize`** [verified]
| Field | Value |
|---|---|
| id | `scale` |
| account | `insidious-kind` |
| namespace | `store` |
| workload kind | `Deployment` |
| workload name | `store-api` |
| scaling_mode | the "set exact replica count" option [UNVERIFIED U10 value name] |
| change_to | `3` |
| min / max | `1` / `3` |
Outputs `old_replicas`, `new_replicas` [verified]. This runs through the in-cluster agent. guardian's `replicas`
in `/api/state` shows 1 → 3 on the cockpit.

### 10. `fs_failover`: **`integrations.http`**, payment_gateway → cashfree
| Field | Value |
|---|---|
| id | `fs_failover` |
| method | `PATCH` |
| url | `<FS_ADMIN>/environments/<FS_ENV_KEY>/featurestates/<FS_ID_payment_gateway>/` |
| headers | `Authorization: Api-Key {{ Secrets.flagsmith_org_key }}`, `Content-Type: application/json` |
| body | `{"feature_state_value": "cashfree"}` |
[UNVERIFIED: remote-config value field name; check with `python3 scripts/flagsmith-toggle.py set payment_gateway=cashfree`.]

### 11. `wait_recovery`: Core task **`core.wait`** [verified]
`duration: 45s` (Go duration). Gives the SDK (5 s refresh) and Prometheus (30 s rate window) time to show
recovery before the "all good" message. Next → `notify_done`.

### 12. `notify_done`: **`integrations.http`** → ops-bot `/nb/notify`
| Field | Value |
|---|---|
| method / url | `POST` `<OPS_BOT>/nb/notify` |
| headers | `Content-Type: application/json`, `X-Insidious-Token: {{ Secrets.festivekit_token }}` |
| body | below |
```json
{
  "text_en": "Done: NudgeBee ran {{ Tasks['decide'].output.selected_branch }} on store-api after your approval. Before: checkout p95 {{ Inputs.event.labels.p95_ms }} ms at {{ Inputs.event.labels.rps }} rps. Big carts kept the full experience. Watch the cockpit for recovery.",
  "severity": "info",
  "voice": true
}
```
ops-bot translates it to Ravi's language ("Sab theek hai…"), speaks it with Bulbul and sends text plus voice.
guardian sends `status: resolved` to the Event Webhook on its own once p95 is healthy for 3 samples.

### 13. `notify_rejected` / `notify_ignored`: **`integrations.http`** → ops-bot `/nb/notify`
Same as 12, with bodies:
- rejected: `{"text_en": "Okay, no change made. Insidious keeps watching checkout and will ask again if it gets worse.", "severity": "warning", "voice": true}`
- ignored: `{"text_en": "NudgeBee looked at the checkout slowdown and decided no action is needed: {{ Tasks['brief'].output.data }}", "severity": "info", "voice": false}`

---

## Test plan (no WhatsApp needed)

1. `make nb-test-event CONFIRM=1` → event + RCA in NudgeBee → this workflow starts on `investigation.completed`.
2. Run status shows `approve` WAITING; ops-bot logs show `/nb/approval-request` (`make logs SVC=ops-bot`).
   With `WHATSAPP_DRY_RUN=1` the message is only logged.
3. Approve from ops-bot (`GET /api/pending`, then the founder flow) or click Approve in the NudgeBee UI.
4. `fs_shed` returns 200 → Flagsmith audit log shows the change made with the org API key → cockpit feed updates.
5. `make nb-test-event STATUS=resolved CONFIRM=1`, then `make reset`.

## Plan B: Workflow Webhook trigger (if the Event trigger misbehaves)

Add a **Webhook** trigger (00-setup.md §4) and point guardian's `NUDGEBEE_EVENT_WEBHOOK_URL` at it. Then:
- replace `rca` with **`llm.investigate`**: message
  `Investigate why checkout is slow for Deployment store-api in namespace store. Alert: {{ Inputs.webhook_json.title }}. Labels: {{ Inputs.webhook_payload }}`, `response_format: text`. Output `{{ Tasks['rca'].output.data }}` [verified].
- replace every `Inputs.event.labels.X` with `Inputs.webhook_json.labels.X` and `Inputs.event.title` with
  `Inputs.webhook_json.title`.
Downside: no NudgeBee event or Investigate page in the video, so use this only if the event path fails.
