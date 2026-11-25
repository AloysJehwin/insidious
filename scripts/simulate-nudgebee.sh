#!/usr/bin/env bash
# Play NudgeBee's part locally: POST what the sale-incident-guard workflow's HTTP tasks send to ops-bot.
# Only talks to your own ops-bot (OPS_BOT_URL, default http://localhost:8003).
#   scripts/simulate-nudgebee.sh approval      # /nb/approval-request (founder gets Approve/Reject)
#   scripts/simulate-nudgebee.sh notify "text" # /nb/notify
set -euo pipefail

OPS="${OPS_BOT_URL:-http://localhost:8003}"
TOKEN="$(python3 scripts/envfile.py get INSIDIOUS_INTERNAL_TOKEN change-me-too)"
kind="${1:-approval}"

case "$kind" in
  approval)
    path=/nb/approval-request
    body='{"execution_id":"","task_id":"","workflow_id":"","action":"shed_heavy_features","summary_en":"Checkout p95 is 1840 ms at 420 rps because store-api is CPU-bound on recommendations. Turning off heavy features raised capacity by 62% in rehearsal.","numbers":{"p95_ms":1840,"rps":420,"gain":"+62%","replicas_to":3}}'
    ;;
  notify)
    path=/nb/notify
    text="${2:-NudgeBee turned off heavy features after your approval. Checkout p95 is back under 800 ms.}"
    body="$(python3 -c 'import json,sys; print(json.dumps({"text_en": sys.argv[1], "severity": "info", "voice": True}))' "$text")"
    ;;
  *) echo "usage: $0 approval|notify [text]"; exit 2 ;;
esac

echo "POST $OPS$path"
curl -sS -X POST "$OPS$path" -H 'Content-Type: application/json' -H "X-Insidious-Token: $TOKEN" -H "X-FestiveKit-Token: $TOKEN" \
  --data "$body" -w '\nHTTP %{http_code}\n'
