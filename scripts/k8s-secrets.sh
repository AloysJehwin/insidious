#!/usr/bin/env bash
# Create/update Secret `insidious-env` from .env in namespaces store + insidious.
# The .env is sanitised first (inline comments/quotes stripped) because kubectl --from-env-file keeps them.
set -euo pipefail

CTX="${KUBE_CONTEXT:?KUBE_CONTEXT must be set}"
ENV_FILE="${ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE not found: cp .env.example .env and fill it in"; exit 1; }

tmp="$(mktemp)"
chmod 600 "$tmp"
trap 'rm -f "$tmp"' EXIT
python3 scripts/envfile.py sanitize "$ENV_FILE" > "$tmp"

for ns in store insidious; do
  kubectl --context "$CTX" create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context "$CTX" apply -f - >/dev/null
  kubectl --context "$CTX" -n "$ns" create secret generic insidious-env \
    --from-env-file="$tmp" --dry-run=client -o yaml \
    | kubectl --context "$CTX" apply -f -
done
echo "secret insidious-env updated in store + insidious (run 'make restart' so running pods pick it up)"
