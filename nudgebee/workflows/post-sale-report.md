# Workflow: `post-sale-report`

After the sale: collect what happened (events, actions, approvals), summarise it for Ravi, add rightsizing/cost
advice if NudgeBee has an hour of history, and send it as a WhatsApp voice note. Cut line: drop it if behind at 3:15.

Placeholders and secrets: [00-setup.md](00-setup.md) §3. Tags: [verified], [nbctl], [UNVERIFIED].

```
trigger: Manual
  events (llm.nubi) → advice (llm.nubi, optional) → summary (llm.summary) → notify (HTTP → ops-bot /nb/notify)
```

## Workflow settings
- Name `post-sale-report`; **Automation Timeout `30m`** (NuBi calls can take minutes).
- Workflow ID → `.env` `NUDGEBEE_WF_POSTSALE_ID`.

## Trigger
**Manual** [verified], inputs:
```json
{"window": "3h", "sale_name": "Diwali Dhamaka"}
```

## Tasks

### 1. `events`: AI task **`llm.nubi`** ("Ask NuBi") [verified]
| Field | Value |
|---|---|
| message | `List every event in the last {{ Inputs.window }} for Deployment store-api in namespace store, especially rule insidious-slo. For each: time, title, root cause, and which workflow actions ran (Flagsmith flag changes, scaling) and who approved them. Plain facts, no markdown tables.` |
| approved_tools | empty (read-only question) |
| account_id | `<ACCOUNT_ID>` |
Output `{{ Tasks['events'].output.data }}`.
Alternative without NuBi: `integrations.http` POST `https://app.nudgebee.com/api/graphql` with
`Authorization: Bearer {{ Secrets.nudgebee_api_token }}` and an `events_list` query (list actions take
`limit/offset/order_by/where` and return `rows` [verified]; exact `where` fields UNVERIFIED).

### 2. `advice` (optional, needs ~1 h of agent history): **`llm.nubi`**
message: `What rightsizing or cost recommendations do you have for Deployment store-api in namespace store? Answer in two short sentences.`
Output `{{ Tasks['advice'].output.data }}`. If history is too short, delete this task and its reference below.

### 3. `summary`: AI task **`llm.summary`** [verified]
message:
```
Write a post-sale report for Ravi, a non-technical shop owner, in at most 3 short sentences and under 400 characters.
Use no double quotes, no newlines and no markdown. Cover: how many slowdowns, what was done and who approved it, and
whether the store is healthy now. Then one tip from the advice.
Facts: {{ Tasks['events'].output.data }}
Advice: {{ Tasks['advice'].output.data }}
```
Output `{{ Tasks['summary'].output.data }}`. The "no double quotes / newlines" rule keeps the JSON body below valid
(templating has no confirmed JSON-escape filter, U6).

### 4. `notify`: **`integrations.http`** → ops-bot
| Field | Value |
|---|---|
| method / url | `POST` `<OPS_BOT>/nb/notify` |
| headers | `Content-Type: application/json`, `X-Insidious-Token: {{ Secrets.festivekit_token }}` |
| body | `{"text_en": "{{ Tasks['summary'].output.data }}", "severity": "info", "voice": true}` |

## Test
Run it manually after one `make festive-rush` cycle. Ravi gets a Hindi voice summary. Keep the summary short
(about 200 characters of voice is about ₹0.40 with Bulbul v4-flash) to save Sarvam credit.
