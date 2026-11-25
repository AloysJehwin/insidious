#!/usr/bin/env bash
# Forward the in-cluster services to the same localhost ports docker compose publishes, so the CLI
# and your browser use identical URLs in both modes. Each forward restarts itself if its pod restarts.
#   store-api 8000 (browser only)   payments-mock 8001   guardian 8002   ops-bot 8003   prometheus 9090
# Load tests must NOT use the store-api forward (kubectl pins it to one pod, so scaling would not help):
# they go through the ingress at http://localhost instead.
set -uo pipefail

CTX="${KUBE_CONTEXT:?KUBE_CONTEXT must be set}"
pids=()

forward() { # ns svc localport remoteport
  ( while true; do
      kubectl --context "$CTX" -n "$1" port-forward "svc/$2" "$3:$4" >/dev/null 2>&1
      sleep 1
    done ) &
  pids+=("$!")
  echo "  http://localhost:$3  ->  $2.$1"
}

cleanup() { kill "${pids[@]}" 2>/dev/null; pkill -P $$ 2>/dev/null; exit 0; }
trap cleanup INT TERM EXIT

echo "port-forwarding via context $CTX (Ctrl-C to stop):"
forward store store-api 8000 8000
forward store payments-mock 8001 8001
forward insidious guardian 8002 8002
forward insidious ops-bot 8003 8003
forward insidious prometheus 9090 9090
echo "  cockpit: http://localhost/ops (ingress) or http://localhost:8000/ops"
wait
