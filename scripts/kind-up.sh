#!/usr/bin/env bash
# Create the kind cluster `insidious` (if missing) and install ingress-nginx for kind.
# Safety: every kubectl call uses an explicit --context. `kind create cluster` switches your current
# kube context; this script switches it back to whatever it was, so nothing else on your machine
# silently starts talking to kind (or vice versa).
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-insidious}"
CTX="kind-${CLUSTER}"
CONFIG="${KIND_CONFIG:-deploy/kind-config.yaml}"
INGRESS_NGINX_VERSION="${INGRESS_NGINX_VERSION:-controller-v1.12.1}"
INGRESS_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/kind/deploy.yaml"

command -v kind >/dev/null || { echo "kind not installed: brew install kind"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl not installed: brew install kubectl"; exit 1; }

prev_ctx="$(kubectl config current-context 2>/dev/null || true)"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "kind cluster '$CLUSTER' already exists"
else
  kind create cluster --name "$CLUSTER" --config "$CONFIG"
fi

if [ -n "$prev_ctx" ] && [ "$prev_ctx" != "$CTX" ]; then
  kubectl config use-context "$prev_ctx" >/dev/null
  echo "restored your current kube context to '$prev_ctx' (Insidious always uses --context $CTX)"
fi

echo "installing ingress-nginx ($INGRESS_NGINX_VERSION) into $CTX"
kubectl --context "$CTX" apply -f "$INGRESS_URL"
echo "waiting for ingress-nginx controller..."
for _ in $(seq 1 30); do
  if kubectl --context "$CTX" -n ingress-nginx get pods -l app.kubernetes.io/component=controller 2>/dev/null | grep -q controller; then
    break
  fi
  sleep 2
done
kubectl --context "$CTX" -n ingress-nginx wait --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=240s
echo "kind cluster ready: context $CTX, ingress on http://localhost"
