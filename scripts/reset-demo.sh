#!/usr/bin/env bash
# Reset demo state between takes: flags back to defaults, store-api back to 1 replica, guardian disarmed.
# Best effort: each step reports and moves on.
#   env: MODE (kind|compose), KUBE_CONTEXT, STORE_URL, GUARDIAN_URL
set -uo pipefail

MODE="${MODE:-compose}"
STORE="${STORE_URL:-http://localhost:8000}"
GUARDIAN="${GUARDIAN_URL:-http://localhost:8002}"
ORG_KEY="$(python3 scripts/envfile.py get FLAGSMITH_ORG_API_KEY)"

echo "1/3 flags -> heavy_features & children ON, payment_gateway=razorpay"
if [ "${SKIP_FLAGS:-0}" = "1" ]; then
  echo "   done by 'insidious reset'"
elif [ -n "$ORG_KEY" ] && [[ "$ORG_KEY" != xxxx* ]]; then
  python3 scripts/flagsmith-toggle.py set heavy_features=on recommendations=on hd_images=on live_reviews=on payment_gateway=razorpay \
    || echo "   Flagsmith reset failed (check keys); continuing"
else
  echo "   no Flagsmith org key: using the store's dev override (/api/dev/flags, needs INSIDIOUS_DEV=1)"
  curl -sS -X POST "$STORE/api/dev/flags" -H 'Content-Type: application/json' \
    -d '{"heavy_features": true, "recommendations": true, "hd_images": true, "live_reviews": true, "payment_gateway": "razorpay"}' \
    -w ' HTTP %{http_code}\n' || echo "   store not reachable at $STORE"
fi

echo "2/3 store-api replicas -> 1"
if [ "$MODE" = "kind" ]; then
  kubectl --context "${KUBE_CONTEXT:?}" -n store scale deployment/store-api --replicas=1
else
  echo "   compose mode: nothing to scale"
fi

echo "3/3 guardian -> disarmed (run 'make guard' to arm and start pre-sale-warmup)"
curl -sS -X POST "$GUARDIAN/api/arm" -H 'Content-Type: application/json' -d '{"armed": false}' \
  -w ' HTTP %{http_code}\n' || echo "   guardian not reachable at $GUARDIAN"

echo "reset done. Wait ~30 s for p95 history to settle before the next take."
