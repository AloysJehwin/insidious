# Workflow: `pre-sale-warmup`

30 minutes before the sale: scale store-api to 3, make sure heavy features are on and the payment gateway is
razorpay, and tell Ravi on WhatsApp. Started by a schedule, by hand, or by `insidious guard` (`make guard`) through
`workflow_execute` [nbctl].

Placeholders and secrets: see [00-setup.md](00-setup.md) §3. Tags: [verified], [nbctl], [UNVERIFIED].

```
triggers: Schedule (cron, UTC) + Manual
  scale_up (k8s.horizontal_rightsize → 3)
  → fs_heavy_on (HTTP PATCH heavy_features enabled=true)
  → fs_gateway_razorpay (HTTP PATCH payment_gateway=razorpay)
  → notify (HTTP → ops-bot /nb/notify, voice)
```

## Workflow settings
- Name `pre-sale-warmup`; **Automation Timeout `30m`** (no approval here, but keep it uniform).
- Copy the workflow ID into `.env` → `NUDGEBEE_WF_PRESALE_ID`.

## Triggers
1. **Schedule** [verified]: 5-field cron in **UTC**. Sale at 20:00 IST = 14:30 UTC; warm-up at 19:30 IST =
   **`0 14 * * *`**. Overlap policy: skip. Catch-up window: off/0. (For the demo you'll usually use Manual.)
2. **Manual** [verified]: inputs JSON (≤2000 chars), used by `insidious guard`:
   ```json
   {"sale_name": "Diwali Dhamaka", "replicas": 3, "source": "insidious guard"}
   ```
   Tasks below use literals (3, "the sale") so the scheduled run, which has no inputs, works too.

## Tasks

### 1. `scale_up`: **`k8s.horizontal_rightsize`** [verified]
| Field | Value |
|---|---|
| account | `insidious-kind` |
| namespace / kind / name | `store` / `Deployment` / `store-api` |
| scaling_mode | "set exact replica count" option [UNVERIFIED value name] |
| change_to | `3` |
| min / max | `1` / `3` |
Outputs `old_replicas`, `new_replicas`.

### 2. `fs_heavy_on`: **`integrations.http`** → Flagsmith Admin API
| Field | Value |
|---|---|
| method | `PATCH` |
| url | `<FS_ADMIN>/environments/<FS_ENV_KEY>/featurestates/<FS_ID_heavy_features>/` |
| headers | `Authorization: Api-Key {{ Secrets.flagsmith_org_key }}`, `Content-Type: application/json` |
| body | `{"enabled": true}` |
Idempotent "make sure it's on". To *read* it instead (true "confirm" step), use
`GET <FS_ADMIN>/environments/<FS_ENV_KEY>/featurestates/?feature_name=heavy_features` and print
`{{ Tasks['fs_check'].output.body }}` with `core.print` [array indexing UNVERIFIED].

### 3. `fs_gateway_razorpay`: **`integrations.http`**
Same as 2 with url `.../featurestates/<FS_ID_payment_gateway>/` and body `{"feature_state_value": "razorpay"}`.

### 4. `notify`: **`integrations.http`** → ops-bot
| Field | Value |
|---|---|
| method / url | `POST` `<OPS_BOT>/nb/notify` |
| headers | `Content-Type: application/json`, `X-Insidious-Token: {{ Secrets.festivekit_token }}` |
| body | below |
```json
{
  "text_en": "Sale starts in 30 minutes. NudgeBee scaled the store from {{ Tasks['scale_up'].output.old_replicas }} to {{ Tasks['scale_up'].output.new_replicas }} servers, heavy features are on, payments use Razorpay, and Insidious is watching checkout speed.",
  "severity": "info",
  "voice": true
}
```

## Test
`make guard` (arms guardian + calls `workflow_execute` with the manual inputs), or **Run** in the builder. Check
`kubectl --context kind-insidious -n store get deploy store-api` shows 3/3, then `make reset` to go back to 1.
