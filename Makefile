# make test            all tests (agent-core + every project)
# make test P=03       one project (prefix match on projects/03-*)
# make arch-check      architecture + agentic-concept conformance (rules R1-R9)
# make eval P=03       run a project's eval cases against its agents (needs an LLM after Milestone 0)
# make demo            agent-core offline demo; make demo P=01 runs a project's demo.py
# make check           test + arch-check
# make up P=01         create/backfill .env (random secrets), build and start a project's Compose services (waits until healthy)
# make gateway-test P=01  build the `test` stage of each image (gateway, mock_apis) and run its tests;
#                      it does not redeploy the running services (run `make up` for that)
# make live-check P=01 call every gateway tool through MCP with a real token; result per tool and configured backend
# make token P=01 [CLIENT=cs-rep|cs-lead]  print a short-lived Keycloak access token (for MCP Inspector)
# make seed-salesforce P=01 [ARGS=--apply]  write synthetic Ridgeline accounts to the Salesforce dev org
# make down P=01       stop a project's Docker Compose services
PY ?= python3
P ?=
PROJECTS := $(if $(P),$(wildcard projects/$(P)*/),$(wildcard projects/*/))

.PHONY: test core-test project-tests arch-check eval check demo up down gateway-test live-check seed-salesforce token

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
	@set -e; for d in $(PROJECTS); do \
		PYTHONPATH=agent-core:$$d $(PY) $$d/evals/run_evals.py; \
	done

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
	CS_REP_CLIENT_SECRET=unused CS_LEAD_CLIENT_SECRET=unused P01_EXPIRY_TEST_CLIENT_SECRET=unused

up:
	$(call for_compose_projects,$(ENSURE_ENV); docker compose -f $$d/docker-compose.yml up -d --build --wait)

down:
	$(call for_compose_projects,$(COMPOSE_DUMMY_SECRETS) docker compose -f $$d/docker-compose.yml --profile ops down)

gateway-test:
	$(call for_compose_projects,for c in gateway mock_apis; do \
		[ -f $$d/$$c/Dockerfile ] || continue; \
		docker build -q --target test -f $$d/$$c/Dockerfile -t ridgeline/$$c:test $$d >/dev/null && \
		docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL ridgeline/$$c:test || exit 1; \
	done)

# live-check and token run in the one-off `ops` container (it holds the agent client secrets).
live-check:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.live_check)

# Prints a short-lived access token to stdout (e.g. for MCP Inspector); errors go to stderr. CLIENT=cs-rep | cs-lead
token:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml --profile ops run --rm --no-deps -T ops python -m ridgeline_gateway.admin.token $(or $(CLIENT),cs-rep))

# Dry run by default; ARGS=--apply writes the synthetic Ridgeline accounts to the Salesforce dev org.
seed-salesforce:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml exec -T gateway python -m ridgeline_gateway.admin.seed_salesforce $(ARGS))
