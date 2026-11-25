#!/usr/bin/env bash
# Send a sample Insidious SLO event to the NudgeBee Event Webhook (what guardian sends on a breach).
# Prints the request by default; only sends with CONFIRM=1.
#   make nb-test-event                    # print
#   make nb-test-event CONFIRM=1          # send "firing" with a fresh id
#   make nb-test-event STATUS=resolved CONFIRM=1   # resolve the last test event
set -euo pipefail

STATUS="${STATUS:-firing}"
SAMPLE="nudgebee/workflows/sample-event.json"
LAST_ID_FILE="${TMPDIR:-/tmp}/insidious-last-test-event-id"

URL="$(python3 scripts/envfile.py get NUDGEBEE_EVENT_WEBHOOK_URL)"
if [ -z "$URL" ] || [[ "$URL" == *"token=xxxx"* ]]; then
  echo "NUDGEBEE_EVENT_WEBHOOK_URL is not set in .env (Admin > Integrations > Webhooks > Event Webhook)"
  [ "${CONFIRM:-0}" = "1" ] && exit 1
  URL="https://app.nudgebee.com/api/webhooks/event?token=<your-token>"
fi

if [ "$STATUS" = "resolved" ] && [ -f "$LAST_ID_FILE" ]; then
  ID="$(cat "$LAST_ID_FILE")"
else
  ID="slo-checkout-p95-$(date +%s)"
fi

PAYLOAD="$(python3 - "$SAMPLE" "$ID" "$STATUS" <<'PY'
import json, sys
path, eid, status = sys.argv[1:4]
ev = json.load(open(path))
ev["id"] = eid
ev["status"] = status
print(json.dumps(ev))
PY
)"

TOKEN="$(printf '%s' "$URL" | sed -n 's/.*[?&]token=\([^&]*\).*/\1/p')"
MASKED="$(python3 scripts/envfile.py mask "$URL")"

echo "POST $MASKED"
echo "$PAYLOAD" | python3 -m json.tool
if [ "${CONFIRM:-0}" != "1" ]; then
  echo
  echo "Not sent. Equivalent command:"
  echo "  curl -sS -X POST '$MASKED' -H 'Content-Type: application/json' -H 'Authorization: Bearer ****' -d '<payload above>'"
  echo "Re-run with CONFIRM=1 to send it."
  exit 0
fi

echo "$ID" > "$LAST_ID_FILE"
curl -sS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  ${TOKEN:+-H "Authorization: Bearer $TOKEN"} \
  --data "$PAYLOAD" -w '\nHTTP %{http_code}\n'
