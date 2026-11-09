# make test            all tests (agent-core + every project)
# make test P=03       one project (prefix match on projects/03-*)
# make arch-check      architecture + agentic-concept conformance (rules R1-R9)
# make eval P=03       run a project's eval cases against its agents (needs an LLM after Milestone 0)
# make demo            agent-core offline demo; make demo P=01 runs a project's demo.py
# make check           test + arch-check
# make up P=01         create .env if missing, build and start a project's Docker Compose services (waits until healthy)
# make gateway-test P=01  build the `test` stage of each image (gateway, mock_apis) and run its tests;
#                      it does not redeploy the running services (run `make up` for that)
# make live-check P=01 call every gateway tool through MCP and report which backend answered
# make seed-salesforce P=01 [ARGS=--apply]  write synthetic Ridgeline accounts to the Salesforce dev org
# make down P=01       stop a project's Docker Compose services
PY ?= python3
P ?=
PROJECTS := $(if $(P),$(wildcard projects/$(P)*/),$(wildcard projects/*/))

.PHONY: test core-test project-tests arch-check eval check demo up down gateway-test live-check seed-salesforce

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

# Creates <project>/.env from .env.example the first time (never overwrites), replacing each
# KEY=__GENERATE__ line with a fresh random secret (aborts if openssl fails, so no empty or repeated
# values). Written to a temp file with owner-only permissions, then moved into place; never printed.
# Keys added to .env.example later are not backfilled into an existing .env: add them by hand.
ENSURE_ENV = if [ -f $$d/.env.example ] && [ ! -f $$d/.env ]; then \
		(umask 077; awk '/^[A-Z0-9_]+=__GENERATE__$$/ { c = "openssl rand -hex 24"; r = ""; \
			if ((c | getline r) <= 0 || length(r) < 32) { print "openssl failed" > "/dev/stderr"; exit 1 }; \
			close(c); sub(/__GENERATE__/, r) } { print }' $$d/.env.example > $$d/.env.tmp) && \
		mv $$d/.env.tmp $$d/.env && echo "created $$d.env from .env.example (random mock-API secrets)" \
		|| { rm -f $$d/.env.tmp; echo "could not create $$d.env"; exit 1; }; fi

up:
	$(call for_compose_projects,$(ENSURE_ENV); docker compose -f $$d/docker-compose.yml up -d --build --wait)

# down needs no secrets; dummy values satisfy the compose file's required-variable checks.
down:
	$(call for_compose_projects,CARRIER_API_KEY=unused AR_API_TOKEN=unused docker compose -f $$d/docker-compose.yml down)

gateway-test:
	$(call for_compose_projects,for c in gateway mock_apis; do \
		[ -f $$d/$$c/Dockerfile ] || continue; \
		docker build -q --target test -f $$d/$$c/Dockerfile -t ridgeline/$$c:test $$d >/dev/null && \
		docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL ridgeline/$$c:test || exit 1; \
	done)

live-check:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml exec -T gateway python -m ridgeline_gateway.admin.live_check)

# Dry run by default; ARGS=--apply writes the synthetic Ridgeline accounts to the Salesforce dev org.
seed-salesforce:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml exec -T gateway python -m ridgeline_gateway.admin.seed_salesforce $(ARGS))
