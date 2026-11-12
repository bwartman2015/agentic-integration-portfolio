# make test            all tests (agent-core + every project)
# make test P=03       one project (prefix match on projects/03-*)
# make arch-check      architecture + agentic-concept conformance (rules R1-R9)
# make eval P=03       run a project's eval cases against its agents (scripted model, offline)
# make eval P=01 LLM=local [ARGS=--update-baseline]  real-model evals in the copilot container (free);
#                      LLM=claude: an optional, paid hosted model (your own ANTHROPIC_API_KEY)
# make demo            agent-core offline demo; make demo P=01 runs a project's demo.py
# make check           test + arch-check
# make up P=01         create/backfill .env (random secrets), build and start a project's Compose services (waits until healthy)
# make gateway-test P=01  build the `test` stage of each image (gateway, mock_apis) and run its tests;
#                      it does not redeploy the running services (run `make up` for that)
# make opa-test P=01   Rego unit tests and format check for a project's policy/ directory, in its pinned OPA image
# make live-check P=01 call every gateway tool through MCP with a real token; result per tool and configured backend
# make token P=01 [CLIENT=cs-rep|cs-lead]  print a short-lived Keycloak access token (for MCP Inspector)
# make audit P=01 [ARGS=--limit=20]  the gateway audit report: every call, allowed or denied, with its reason
# make approvals P=01  write requests waiting for a human decision, with their approval links
# make up P=01 DB=test|local  the stack on the Neon `test` branch or the local Postgres (default: .env's
#                      DATABASE_URL, i.e. Neon main when set). Wire tests refuse to run against `main`.
# make db-init P=01 [TARGET=test]   hosted database (Neon): apply db/audit.sql and db/memory.sql, stamp it
# make db-roles P=01 [TARGET=test]  hosted database: create/rotate gateway_audit and copilot_memory, update .env
# make models P=01 [MODEL=chat]  download the embedding (or local chat) model, checksum-verified, into a volume
# make ask P=01 Q="..." [ROLE=cs-lead] [LLM=local|claude]  ask the CS copilot (container): gateway tools, pgvector memory
# make a2a-up P=01     the copilot's A2A endpoint (Agent Card + message/send) on 127.0.0.1:8701
# make a2a-check P=01  fetch the card and send a task as cs-rep over A2A (real Keycloak token)
# make console-up P=01    the local web console on http://localhost:8790 (loopback only)
# make console-check P=01 one question through the console on a test stack: answer, tool calls, audit rows
# make copilot-test P=01  build the copilot image's test stage and run its tests (real MCP SDK, no network)
# make request-case P=01  as cs-lead, request a CRM case and wait for the human decision in n8n
# make seed-salesforce P=01 [ARGS=--apply]  write synthetic Ridgeline accounts to the Salesforce dev org
# make down P=01       stop a project's Docker Compose services
PY ?= python3
P ?=
DB ?=
TARGET ?= main
ifneq ($(filter-out test local,$(DB)),)
$(error DB must be test or local (empty: the database .env names))
endif
LLM ?=
ifneq ($(filter-out claude local,$(LLM)),)
$(error LLM must be claude or local (empty: the scripted model))
endif
# LLM=claude|local -> the copilot's LLM_PROVIDER
LLM_PROVIDER_claude = anthropic
LLM_PROVIDER_local = local
MODEL ?= embeddings
# Compose files for the selected database (DB=test|local adds an override; see docker-compose.db-*.yml).
COMPOSE = docker compose -f $$d/docker-compose.yml $(if $(DB),-f $$d/docker-compose.db-$(DB).yml)
PROJECTS := $(if $(P),$(wildcard projects/$(P)*/),$(wildcard projects/*/))

.PHONY: test core-test project-tests arch-check eval check demo up down gateway-test copilot-test opa-test live-check seed-salesforce token audit approvals request-case db-init db-roles models ask a2a-up a2a-check console-up console-check

test: core-test project-tests

core-test:
	cd agent-core && $(PY) -m unittest discover -s tests

project-tests:
	@set -e; for d in $(PROJECTS); do \
		echo "== $$d"; \
		PYTHONPATH=agent-core:$$d $(PY) -m unittest discover -s $$d/tests; \
	done

arch-check:
	$(PY) scripts/arch_check.py $(PROJECTS)

eval:
	@test -n "$(P)" || (echo "usage: make eval P=03" && exit 1)
ifeq ($(LLM),)
	@set -e; for d in $(PROJECTS); do \
		PYTHONPATH=agent-core:$$d $(PY) $$d/evals/run_evals.py $(ARGS); \
	done
else
	$(call for_compose_projects,[ -f $$d/copilot/Dockerfile ] || { echo "no copilot image in $$d"; continue; }; \
		$(ENSURE_ENV) >/dev/null; \
		$(COMPOSE) --progress quiet --profile copilot build copilot && \
		$(LOCAL_LLM_UP) \
		EVAL_LLM_PROVIDER=$(LLM_PROVIDER_$(LLM)) $(COMPOSE) --profile eval run --rm --no-deps -T eval $(ARGS); \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)
endif

# LLM=local: make sure the chat model is downloaded and its server is up (a no-op for other models).
LOCAL_LLM_UP = $(if $(filter local,$(LLM)),$(COMPOSE) --profile admin run --rm --no-deps -T models chat && \
	$(COMPOSE) --profile local-llm up -d --wait llm &&)

demo:
ifeq ($(P),)
	cd agent-core && PYTHONPATH=. $(PY) examples/demo.py
else
	@set -e; test -n "$(PROJECTS)" || { echo "no project matches P=$(P)"; exit 1; }; \
	for d in $(PROJECTS); do \
		if [ ! -f $$d/demo.py ]; then echo "no demo.py in $$d yet"; continue; fi; \
		PYTHONPATH=agent-core:$$d $(PY) $$d/demo.py; \
	done
endif

check: test arch-check

# Optional machine-local targets (not part of the repository).
-include local.mk

# Docker Compose per project (Docker Desktop). Projects without a docker-compose.yml are skipped.
define for_compose_projects
	@test -n "$(P)" || (echo "usage: make $@ P=01" && exit 1)
	@set -e; test -n "$(PROJECTS)" || { echo "no project matches P=$(P)"; exit 1; }; \
	for d in $(PROJECTS); do \
		if [ ! -f $$d/docker-compose.yml ]; then echo "no docker-compose.yml in $$d"; continue; fi; \
		$(1); \
	done
endef

# Creates or backfills <project>/.env from .env.example (scripts/ensure-env.sh): random values for every
# KEY=__GENERATE__ key that .env lacks; existing values are never changed or printed.
ENSURE_ENV = scripts/ensure-env.sh $$d

# Compose interpolates required secrets even for `down`; dummy values satisfy those checks.
COMPOSE_DUMMY_SECRETS = CARRIER_API_KEY=unused AR_API_TOKEN=unused KC_ADMIN_PASSWORD=unused \
	CS_REP_CLIENT_SECRET=unused CS_LEAD_CLIENT_SECRET=unused P01_EXPIRY_TEST_CLIENT_SECRET=unused \
	P01_CRM_ONLY_TEST_CLIENT_SECRET=unused P01_RATE_TEST_CLIENT_SECRET=unused AUDIT_DB_ADMIN_PASSWORD=unused \
	AUDIT_DB_PASSWORD=unused N8N_ENCRYPTION_KEY=unused APPROVAL_OPERATOR_TOKEN=unused \
	APPROVAL_WEBHOOK_SECRET=unused P01_AUDITOR_CLIENT_SECRET=unused COPILOT_MEMORY_DB_PASSWORD=unused \
	CONSOLE_TOKEN=unused

# Each rebuild leaves the previous image untagged; delete those (only this repo's, by label) so local
# image storage stays flat on a laptop's Docker Desktop.
PRUNE_OLD_BUILDS = docker image prune -f --filter label=org.ridgeline.project=ridgeline-portfolio >/dev/null

up:
	$(call for_compose_projects,$(ENSURE_ENV); status=0; \
		$(COMPOSE) up -d --build --wait --remove-orphans || status=$$?; \
		$(PRUNE_OLD_BUILDS); [ $$status -eq 0 ] || exit $$status)

down:
	$(call for_compose_projects,$(COMPOSE_DUMMY_SECRETS) docker compose -f $$d/docker-compose.yml --profile ops --profile copilot down)

gateway-test:
	$(call for_compose_projects,for c in gateway mock_apis; do \
		[ -f $$d/$$c/Dockerfile ] || continue; \
		docker build -q --target test -f $$d/$$c/Dockerfile -t ridgeline/$$c:test $$d >/dev/null && \
		docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL ridgeline/$$c:test || exit 1; \
	done; $(PRUNE_OLD_BUILDS))

# The copilot image's test stage (needs agent-core as a named build context), with no network.
copilot-test:
	$(call for_compose_projects,[ -f $$d/copilot/Dockerfile ] || { echo "no copilot/Dockerfile in $$d"; continue; }; \
		docker build -q --target test --build-context agentcore=agent-core -f $$d/copilot/Dockerfile \
			-t ridgeline/cs-copilot:test $$d >/dev/null && \
		docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL ridgeline/cs-copilot:test; \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)

# Rego unit tests (`opa test`) and a format check (`opa fmt`), in the OPA image the project's Compose file pins.
opa-test:
	$(call for_compose_projects,[ -d $$d/policy ] || { echo "no policy/ in $$d"; continue; }; \
		image=$$(sed -n 's/^ *image: *\(openpolicyagent\/opa[^ ]*\).*/\1/p' $$d/docker-compose.yml | head -1); \
		test -n "$$image" || { echo "no pinned OPA image in $$d/docker-compose.yml"; exit 1; }; \
		run="docker run --rm --network none --read-only --cap-drop ALL -v $$(cd $$d && pwd)/policy:/policy:ro $$image"; \
		$$run test /policy -v && $$run fmt --fail --list /policy)

# live-check and token run in the one-off `ops` container (it holds the agent client secrets).
live-check:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.live_check)

# Prints a short-lived access token to stdout (e.g. for MCP Inspector); errors go to stderr. CLIENT=cs-rep | cs-lead
token:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.token $(or $(CLIENT),cs-rep))

# The audit report: totals by outcome and reason, recent calls and approvals (read as cs-lead, who holds audit.read).
audit:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.audit_report $(ARGS))

# Write requests waiting for a human decision, with their approval links (operator token; no agent holds it).
approvals:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.approvals)

# One time, for a hosted database (Neon): apply db/audit.sql and db/memory.sql with DATABASE_ADMIN_URL
# (else DATABASE_URL_UNPOOLED, else DATABASE_URL).
db-init:
	$(call for_compose_projects,DB_TARGET=$(TARGET) docker compose -f $$d/docker-compose.yml --profile admin run --rm --no-deps -T db-init)

# For a hosted database: create or rotate the least-privilege gateway_audit and copilot_memory roles and
# point DATABASE_URL and MEMORY_DATABASE_URL in .env at them (no password is printed). Needs
# DATABASE_ADMIN_URL or DATABASE_URL_UNPOOLED.
db-roles:
	$(call for_compose_projects,DB_TARGET=$(TARGET) $(PY) $$d/db/roles.py)

# Download the embedding model (nomic-embed-text-v1.5, SHA-256 verified) into the `models` volume.
# Builds the copilot image if needed; a present, matching file is not downloaded again.
models:
	$(call for_compose_projects,$(ENSURE_ENV); \
		docker compose -f $$d/docker-compose.yml --progress quiet --profile copilot build copilot && \
		docker compose -f $$d/docker-compose.yml --profile admin run --rm --no-deps -T models $(MODEL); \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)

# Ask the CS copilot one question (needs `make up P=01` running and `make models P=01` once).
# Q="--list" shows the questions the scripted model knows (until Milestone 7). ROLE=cs-rep | cs-lead.
# The question and role reach the container through the environment (ASK_Q, ASK_ROLE), never through
# shell parsing.
ask: export ASK_Q = $(Q)
ask: export ASK_ROLE = $(ROLE)
ask:
	@test -n "$$ASK_Q" || { echo 'usage: make ask P=01 Q="your question" [ROLE=cs-lead]   (Q=--list for examples)'; exit 1; }
	$(call for_compose_projects,$(COMPOSE) --progress quiet --profile copilot build copilot && \
		$(LOCAL_LLM_UP) \
		COPILOT_LLM_PROVIDER=$(or $(LLM_PROVIDER_$(LLM)),scripted) $(COMPOSE) --profile copilot run --rm -T copilot \
			--role "$${ASK_ROLE:-cs-rep}" $(if $(filter-out --list,$(Q)),"$$ASK_Q",--list); \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)

# The copilot's A2A endpoint (needs `make up P=01` running). LLM=claude|local picks its model.
a2a-up:
	$(call for_compose_projects,$(COMPOSE) --progress quiet --profile copilot build copilot && \
		$(LOCAL_LLM_UP) \
		COPILOT_LLM_PROVIDER=$(or $(LLM_PROVIDER_$(LLM)),scripted) $(COMPOSE) --profile a2a up -d --wait a2a; \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)

# The local web console (needs `make up P=01` running): http://localhost:8790. Starts the local model too.
console-up:
	$(call for_compose_projects,$(ENSURE_ENV) >/dev/null; \
		$(COMPOSE) --progress quiet --profile copilot build copilot && \
		$(COMPOSE) --profile admin run --rm --no-deps -T models chat && \
		$(COMPOSE) --profile console up -d --wait console llm && \
		$(COMPOSE) --profile console exec -T console python -c \
			'import os; print("open this link to sign in (keep it private): http://localhost:" + os.environ.get("CONSOLE_PORT", "8790") + "/?token=" + os.environ["CONSOLE_TOKEN"])'; \
		status=$$?; $(PRUNE_OLD_BUILDS); exit $$status)

# One question through the console, as the page asks it (runs inside the console container, which holds
# the console token; test stack only).
console-check:
	$(call for_compose_projects,$(COMPOSE) --profile console exec -T console python -m cs_copilot.console.check)

# Fetch the card, then send a task as cs-rep with a real Keycloak token (runs in the copilot container).
a2a-check:
	$(call for_compose_projects,$(COMPOSE) --profile copilot run --rm --no-deps -T --entrypoint python copilot \
		-m cs_copilot.a2a_check)

# As cs-lead, ask the gateway to create a case and wait for a human to approve or reject it in n8n.
request-case:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.request_case $(ARGS))

# Dry run by default; ARGS=--apply writes the synthetic Ridgeline accounts to the Salesforce dev org.
seed-salesforce:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml exec -T gateway python -m ridgeline_gateway.admin.seed_salesforce $(ARGS))
