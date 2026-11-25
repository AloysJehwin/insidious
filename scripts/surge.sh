#!/usr/bin/env bash
# Insidious demo controls: start/stop a festive traffic surge on the k3s box, watch it, reset to normal.
#
#   scripts/surge.sh start [peak_rps] [hold]   # default 70 rps for 420s (k6 runs ON the EC2 box, hitting Traefik)
#   scripts/surge.sh stop                      # stop the surge now
#   scripts/surge.sh status                    # one-line live view: rps, checkout p95, incident, flags, replicas
#   scripts/surge.sh watch                     # status every 5 s until Ctrl-C
#   scripts/surge.sh reset                     # normal mode: heavy_features ON, store-api 1 replica, db_pool default
#
# Config (env overrides): FK_HOST, FK_SSH_KEY, FK_KUBECONFIG, FK_CONTEXT. Flagsmith keys are read from ../.env.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FK_HOST="${FK_HOST:-ubuntu@16.4.42.49}"
FK_SSH_KEY="${FK_SSH_KEY:-$HOME/.ssh/festivekit-mumbai.pem}"
FK_KUBECONFIG="${FK_KUBECONFIG:-$HOME/.kube/festivekit-k3s.yaml}"
FK_CONTEXT="${FK_CONTEXT:-festivekit-k3s}"
STATE_URL="${FK_STATE_URL:-http://${FK_HOST#*@}/ops/state}"

ssh_box() { ssh -i "$FK_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 "$FK_HOST" "$@"; }
kc() { kubectl --kubeconfig "$FK_KUBECONFIG" --context "$FK_CONTEXT" "$@"; }

status_line() {
  curl -s -m 6 "$STATE_URL" | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)["state"]
except Exception:
    print("state unavailable (store busy?)"); sys.exit(0)
c = (s.get("slo") or {}).get("/checkout") or {}
inc = s.get("incident") or {}
db = ((s.get("db") or {}).get("primary") or {})
p95 = c.get("p95_ms")
print("rps=%-6s checkout_p95=%-8s target=%sms  incident=%-8s heavy_features=%-5s replicas=%s  db_in_use=%s/%s" % (
    s.get("rps"), "%.0fms" % p95 if isinstance(p95, (int, float)) else "-", c.get("target_p95_ms"),
    inc.get("status") or "none", (s.get("flags") or {}).get("heavy_features"),
    (s.get("replicas") or {}).get("store-api"), db.get("in_use"), db.get("capacity") or db.get("pool_size")))
'
}

cmd="${1:-help}"
case "$cmd" in
  start)
    rps="${2:-70}"; hold="${3:-420s}"
    ssh_box "sudo docker rm -f rush >/dev/null 2>&1 || true; cd ~/festivekit && sudo docker run -d --rm --name rush --network host \
      -v \$PWD/loadtest:/scripts -e STORE_URL=http://localhost -e PEAK_RPS=$rps -e HOLD=$hold \
      grafana/k6:latest run -q /scripts/festive-rush.js >/dev/null"
    echo "surge started: ramp to ${rps} rps, hold ${hold}. Watch: $0 watch   (dashboard: https://festive.aloysjehwin.com/ops)"
    ;;
  stop)
    ssh_box "sudo docker rm -f rush >/dev/null 2>&1 && echo 'surge stopped' || echo 'no surge running'"
    ;;
  status)
    status_line
    ;;
  watch)
    while true; do printf '%s  ' "$(date +%H:%M:%S)"; status_line; sleep 5; done
    ;;
  reset)
    set -a; . "$ROOT/.env"; set +a
    fs_set() {
      local id
      id=$(curl -s "$FLAGSMITH_ADMIN_URL/environments/$FLAGSMITH_ENVIRONMENT_KEY/featurestates/?feature_name=$1" \
        -H "Authorization: Api-Key $FLAGSMITH_ORG_API_KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin); rows = d.get("results", d)
print([r for r in rows if not r.get("identity") and not r.get("feature_segment")][0]["id"])')
      curl -s -o /dev/null -w "  flagsmith $1 -> %{http_code}\n" -X PATCH \
        "$FLAGSMITH_ADMIN_URL/environments/$FLAGSMITH_ENVIRONMENT_KEY/featurestates/$id/" \
        -H "Authorization: Api-Key $FLAGSMITH_ORG_API_KEY" -H "content-type: application/json" -d "$2"
    }
    "$0" stop || true
    fs_set heavy_features '{"enabled": true}'
    for f in recommendations hd_images live_reviews; do fs_set "$f" '{"enabled": true}'; done
    fs_set read_from_replica '{"enabled": false}'
    fs_set db_pool '{"feature_state_value": "{\"pool_size\":5,\"max_overflow\":0,\"timeout_s\":2}"}'
    kc -n store scale deploy/store-api --replicas=1
    echo "reset to normal mode (allow ~30 s for p95 to settle and old incidents to resolve)"
    ;;
  *)
    sed -n '2,10p' "$0"
    ;;
esac
