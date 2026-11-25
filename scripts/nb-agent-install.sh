#!/usr/bin/env bash
# Install the NudgeBee k8s agent on the kind cluster ONLY.
# NudgeBee's installation.sh uses kubectl/helm with the *current* context. To make sure it can never
# touch another cluster, it runs with KUBECONFIG pointing at a file that contains only kind-insidious.
#   make nb-agent-install NB_AGENT_KEY=<key from Admin > Integrations > Kubernetes Clusters > Add K8s Account>
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-insidious}"
KEY="${NB_AGENT_KEY:?set NB_AGENT_KEY (agent auth key from the NudgeBee UI)}"
KCFG="${HOME}/.kube/kind-${CLUSTER}.kubeconfig"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v kind >/dev/null || { echo "kind not installed"; exit 1; }
mkdir -p "$(dirname "$KCFG")"
kind get kubeconfig --name "$CLUSTER" > "$KCFG"
chmod 600 "$KCFG"
echo "isolated kubeconfig: $KCFG (contexts: $(KUBECONFIG="$KCFG" kubectl config get-contexts -o name | tr '\n' ' '))"

curl -fsSL -o "$WORK/installation.sh" https://raw.githubusercontent.com/nudgebee/k8s-agent/main/installation.sh
echo "downloaded installation.sh ($(wc -c < "$WORK/installation.sh") bytes) to $WORK; review it if you like, then press Enter"
read -r _
# -d true disables the eBPF node agent, which cannot attach on kind.
( cd "$WORK" && KUBECONFIG="$KCFG" bash installation.sh -a "$KEY" -d true )
KUBECONFIG="$KCFG" kubectl get pods -n nudgebee-agent
