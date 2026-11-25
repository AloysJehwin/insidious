# Insidious Makefile. `make` (or `make help`) lists targets.
#
# SAFETY: every kubectl call uses an explicit --context $(KUBE_CONTEXT) (default kind-insidious).
# Nothing relies on your default kube context. Cluster targets refuse non-kind contexts unless
# ALLOW_NON_KIND=1 (for EKS). kind commands use --name $(KIND_CLUSTER).
#
# MODE is auto-detected (kind if the ingress answers on http://localhost, else compose); override with
# `make <target> MODE=kind|compose`. Host-side tools (CLI, k6) get localhost URLs for that mode.

SHELL := /bin/bash
.DEFAULT_GOAL := help

KUBE_CONTEXT   ?= kind-insidious
KIND_CLUSTER   ?= insidious
ALLOW_NON_KIND ?= 0
IMAGE_PREFIX   ?= insidious
TAG            ?= dev
SERVICES       := store-api payments-mock guardian ops-bot
COMPOSE        ?= docker compose
KUBECTL         = kubectl --context $(KUBE_CONTEXT)

MODE             ?= $(shell bash scripts/detect-mode.sh)
STORE_PUBLIC_URL ?= $(if $(filter kind,$(MODE)),http://localhost,http://localhost:8000)
NGROK_PORT       ?= $(if $(filter kind,$(MODE)),80,8003)
NGROK_DOMAIN     ?=
INSIDIOUS       ?= $(if $(wildcard .venv/bin/insidious),.venv/bin/insidious,insidious)

# kind: init also applies deploy/k8s with an explicit context (the CLI refuses without --context)
INIT_ARGS          ?= $(if $(filter kind,$(MODE)),--apply --context $(KUBE_CONTEXT),)
REHEARSE_ARGS      ?=
QUICK_REHEARSE_ARGS ?= --quick
GUARD_ARGS         ?=
# festive-rush knobs (loadtest/festive-rush.js): make festive-rush PEAK_RPS=800 HOLD=120s
PEAK_RPS           ?=
HOLD               ?=
K6_ARGS            ?= $(if $(PEAK_RPS),-e PEAK_RPS=$(PEAK_RPS)) $(if $(HOLD),-e HOLD=$(HOLD))
STATUS             ?= firing
CONFIRM            ?= 0
SVC                ?= ops-bot
NS                  = $(if $(filter store-api payments-mock,$(SVC)),store,insidious)

# Env for host-side tools: same localhost ports in compose (published) and kind (make port-forward).
HOST_ENV = MODE=$(MODE) INSIDIOUS_MODE=$(MODE) KUBE_CONTEXT=$(KUBE_CONTEXT) \
	STORE_URL=$(STORE_PUBLIC_URL) PAYMENTS_URL=http://localhost:8001 GUARDIAN_URL=http://localhost:8002 \
	OPS_BOT_URL=http://localhost:8003 PROMETHEUS_URL=http://localhost:9090 \
	INSIDIOUS_CONFIG=$(CURDIR)/insidious.yaml INSIDIOUS_FLAGS=$(CURDIR)/flagsmith/flags.yaml

.PHONY: help dotenv venv test build compose-up compose-down compose-logs compose-rush \
	kind-up kind-down kind-load context-guard config-sync k8s-render secrets deploy restart up undeploy \
	port-forward ngrok logs init rehearse quick-rehearse festive-rush break-demo guard status reset \
	shed fs-ids nb-test-event nb-agent-install simulate-approval simulate-notify

##@ General
help: ## Show this help
	@echo "Insidious (KUBE_CONTEXT=$(KUBE_CONTEXT), KIND_CLUSTER=$(KIND_CLUSTER))"
	@awk 'BEGIN {FS = ":.*?## "} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "Three commands: make init | make rehearse | make guard     (demo: make festive-rush)"

dotenv: ## Create .env from .env.example if missing
	@if [ -f .env ]; then echo ".env exists"; else cp .env.example .env && echo "created .env -- fill in keys (or set *_MOCK/*_DRY_RUN=1)"; fi

venv: ## Create .venv with sdk, cli, service deps and pytest
	@PY=$$(command -v python3.12 || command -v python3); echo "using $$PY"; \
	[ -d .venv ] || $$PY -m venv .venv; \
	.venv/bin/pip install -q --upgrade pip; \
	.venv/bin/pip install -q pytest pytest-asyncio respx; \
	for d in sdk cli; do \
	  if [ -f $$d/pyproject.toml ] || [ -f $$d/setup.py ]; then echo "pip install -e ./$$d"; .venv/bin/pip install -q -e ./$$d || exit 1; fi; \
	done; \
	for r in services/*/requirements.txt; do \
	  if [ -f $$r ]; then echo "pip install -r $$r"; .venv/bin/pip install -q -r $$r || exit 1; fi; \
	done; \
	echo "done: source .venv/bin/activate"

test: ## Run pytest for sdk, cli and every service that has tests/
	@bash scripts/test-all.sh

build: ## docker build the 4 images (repo root context) as insidious/<svc>:dev
	@missing=""; for s in $(SERVICES); do \
	  d=services/$$s; [ $$s = store-api ] && d=services/store; \
	  if [ ! -f $$d/Dockerfile ]; then echo "!! $$d/Dockerfile missing, skipping $$s"; missing="$$missing $$s"; continue; fi; \
	  echo ">> docker build -f $$d/Dockerfile -t $(IMAGE_PREFIX)/$$s:$(TAG) ."; \
	  docker build -f $$d/Dockerfile -t $(IMAGE_PREFIX)/$$s:$(TAG) . || exit 1; \
	done; \
	if [ -n "$$missing" ]; then echo "not built:$$missing"; exit 1; fi

##@ Local (docker compose, all mocks, no accounts)
compose-up: ## Build and start store, payments, guardian, ops-bot, prometheus
	$(COMPOSE) up -d --build
	@echo "store  http://localhost:8000   cockpit http://localhost:8000/ops   prometheus http://localhost:9090"

compose-down: ## Stop the compose stack
	$(COMPOSE) down

compose-logs: ## Follow compose logs (SVC=<name> for one service)
	$(COMPOSE) logs -f --tail=100 $(if $(filter command line,$(origin SVC)),$(SVC),)

compose-rush: ## Run festive-rush from the k6 container (no local k6 needed)
	$(COMPOSE) --profile load run --rm k6

##@ Kubernetes (kind)
kind-up: ## Create kind cluster 'insidious' (1 cp + 2 workers) + ingress-nginx
	@KIND_CLUSTER=$(KIND_CLUSTER) bash scripts/kind-up.sh

kind-down: ## Delete the kind cluster (also delete the K8s account in the NudgeBee UI)
	kind delete cluster --name $(KIND_CLUSTER)

kind-load: ## Load insidious/<svc>:dev images into the kind nodes
	@for s in $(SERVICES); do echo ">> kind load $(IMAGE_PREFIX)/$$s:$(TAG)"; kind load docker-image $(IMAGE_PREFIX)/$$s:$(TAG) --name $(KIND_CLUSTER) || exit 1; done

context-guard: ## Abort unless KUBE_CONTEXT is kind-* (set ALLOW_NON_KIND=1 for EKS)
	@case "$(KUBE_CONTEXT)" in \
	  kind-*) ;; \
	  *) if [ "$(ALLOW_NON_KIND)" != "1" ]; then \
	       echo "REFUSING: KUBE_CONTEXT=$(KUBE_CONTEXT) is not a kind-* context. Set ALLOW_NON_KIND=1 to target it on purpose (e.g. EKS)."; exit 1; \
	     fi; \
	     echo "WARNING: targeting non-kind context $(KUBE_CONTEXT) (ALLOW_NON_KIND=1)";; \
	esac
	@kubectl config get-contexts -o name 2>/dev/null | grep -qx "$(KUBE_CONTEXT)" || { echo "kube context $(KUBE_CONTEXT) not found in your kubeconfig. Run 'make kind-up' first."; exit 1; }

config-sync: ## Copy insidious.yaml + flags.yaml into deploy/k8s/generated (kustomize can't read ../)
	@mkdir -p deploy/k8s/generated
	@cp insidious.yaml deploy/k8s/generated/insidious.yaml
	@cp flagsmith/flags.yaml deploy/k8s/generated/flags.yaml

k8s-render: config-sync ## Render deploy/k8s offline (kubectl kustomize; touches no cluster)
	kubectl kustomize deploy/k8s

secrets: context-guard ## Secret insidious-env from .env in ns store + insidious
	@KUBE_CONTEXT=$(KUBE_CONTEXT) bash scripts/k8s-secrets.sh

deploy: context-guard config-sync ## Apply secrets + deploy/k8s to KUBE_CONTEXT, restart pods
	@if [ -f .env ]; then KUBE_CONTEXT=$(KUBE_CONTEXT) bash scripts/k8s-secrets.sh; else echo "!! no .env: pods start without Secret insidious-env"; fi
	$(KUBECTL) apply -k deploy/k8s
	@$(MAKE) --no-print-directory restart

restart: context-guard ## Rolling restart (picks up new :dev images and secret changes)
	$(KUBECTL) -n store rollout restart deployment/store-api deployment/payments-mock
	$(KUBECTL) -n insidious rollout restart deployment/guardian deployment/ops-bot
	$(KUBECTL) -n store rollout status deployment/store-api --timeout=180s
	$(KUBECTL) -n store rollout status deployment/payments-mock --timeout=180s
	$(KUBECTL) -n insidious rollout status deployment/guardian --timeout=180s
	$(KUBECTL) -n insidious rollout status deployment/ops-bot --timeout=180s
	$(KUBECTL) -n insidious rollout status deployment/prometheus --timeout=180s

up: kind-up build kind-load deploy ## Everything for kind: cluster, images, load, deploy
	@echo "next: make port-forward (separate terminal), make ngrok, make init"

undeploy: context-guard ## Delete everything in deploy/k8s from KUBE_CONTEXT
	$(KUBECTL) delete -k deploy/k8s --ignore-not-found

port-forward: context-guard ## Forward guardian/ops-bot/prometheus/store to localhost:8000-8003,9090
	@KUBE_CONTEXT=$(KUBE_CONTEXT) bash scripts/port-forward.sh

logs: context-guard ## Follow pod logs in kind (SVC=store-api|payments-mock|guardian|ops-bot|prometheus)
	$(KUBECTL) -n $(NS) logs -f deployment/$(SVC) --tail=100

nb-agent-install: ## Install the NudgeBee agent on kind only (NB_AGENT_KEY=...), isolated kubeconfig
	@KIND_CLUSTER=$(KIND_CLUSTER) bash scripts/nb-agent-install.sh

ngrok: ## Public URL: ngrok static domain -> ingress :80 (kind) or ops-bot :8003 (compose)
	@dom="$(NGROK_DOMAIN)"; \
	if [ -z "$$dom" ]; then dom=$$(python3 scripts/envfile.py get PUBLIC_BASE_URL | sed -E 's#^https?://##; s#/.*$$##'); fi; \
	if [ -z "$$dom" ] || [ "$$dom" = "your-name.ngrok-free.app" ]; then echo "set NGROK_DOMAIN=<name>.ngrok-free.app or PUBLIC_BASE_URL in .env"; exit 1; fi; \
	echo ">> ngrok http $(NGROK_PORT) --url=$$dom   (older ngrok: --domain=$$dom)"; \
	ngrok http $(NGROK_PORT) --url=$$dom

##@ The three commands (run on your laptop; uses localhost URLs)
init: config-sync ## insidious init: flags + segments, k8s apply (kind), NudgeBee checks
	@if [ "$(MODE)" = kind ]; then $(MAKE) --no-print-directory context-guard; fi
	$(HOST_ENV) $(INSIDIOUS) init $(INIT_ARGS)

rehearse: ## insidious rehearse: two-pass flag-aware load test + readiness report + voice brief
	$(HOST_ENV) $(INSIDIOUS) rehearse $(REHEARSE_ARGS)

quick-rehearse: ## Shorter rehearse for demo takes (QUICK_REHEARSE_ARGS, default --quick)
	$(HOST_ENV) $(INSIDIOUS) rehearse $(QUICK_REHEARSE_ARGS)

guard: ## insidious guard: arm guardian + run pre-sale-warmup workflow
	$(HOST_ENV) $(INSIDIOUS) guard $(GUARD_ARGS)

status: ## insidious status (+ pods when MODE=kind)
	-$(HOST_ENV) $(INSIDIOUS) status
	@if [ "$(MODE)" = kind ]; then $(MAKE) --no-print-directory context-guard && \
	  $(KUBECTL) -n store get deploy,pods -o wide && $(KUBECTL) -n insidious get deploy,pods; fi

##@ Demo
festive-rush: ## Spike traffic with k6 (loadtest/festive-rush.js); kind: via ingress so all pods get load
	@command -v k6 >/dev/null || { echo "k6 not installed (brew install k6); or: make compose-rush"; exit 1; }
	@test -f loadtest/festive-rush.js || { echo "loadtest/festive-rush.js not written yet"; exit 1; }
	@echo ">> k6 against $(STORE_PUBLIC_URL) (MODE=$(MODE))"
	k6 run -e STORE_URL=$(STORE_PUBLIC_URL) -e BASE_URL=$(STORE_PUBLIC_URL) $(K6_ARGS) loadtest/festive-rush.js

break-demo: festive-rush ## Alias for festive-rush

reset: ## Between takes: flags to defaults, store-api 1 replica, guardian disarmed
	@if [ "$(MODE)" = kind ]; then $(MAKE) --no-print-directory context-guard; fi
	@skip=0; if command -v $(INSIDIOUS) >/dev/null 2>&1; then $(HOST_ENV) $(INSIDIOUS) reset && skip=1; fi; \
	$(HOST_ENV) SKIP_FLAGS=$$skip bash scripts/reset-demo.sh

shed: ## Manual fallback: turn heavy_features OFF in Flagsmith (Admin API, org key)
	python3 scripts/flagsmith-toggle.py set heavy_features=off

fs-ids: ## Print env-level Flagsmith featurestate ids (for the NudgeBee HTTP tasks)
	python3 scripts/flagsmith-toggle.py ids

nb-test-event: ## Print a sample SLO event for the NudgeBee Event Webhook; CONFIRM=1 sends (STATUS=resolved)
	@STATUS=$(STATUS) CONFIRM=$(CONFIRM) bash scripts/nb-test-event.sh

simulate-approval: ## Play NudgeBee locally: POST /nb/approval-request to ops-bot (localhost:8003)
	@OPS_BOT_URL=http://localhost:8003 bash scripts/simulate-nudgebee.sh approval

simulate-notify: ## Play NudgeBee locally: POST /nb/notify to ops-bot (localhost:8003)
	@OPS_BOT_URL=http://localhost:8003 bash scripts/simulate-nudgebee.sh notify
