# make test            all tests (agent-core + every project)
# make test P=03       one project (prefix match on projects/03-*)
# make arch-check      architecture + agentic-concept conformance (rules R1-R9)
# make eval P=03       run a project's eval cases against its agents (needs an LLM after Milestone 0)
# make demo            agent-core offline demo; make demo P=01 runs a project's demo.py
# make check           test + arch-check
# make up P=01         build and start a project's Docker Compose services (waits until healthy)
# make gateway-test P=01  build a project's MCP gateway test image (Dockerfile stage `test`) and run its tests;
#                      it does not redeploy the running gateway (run `make up` for that)
# make down P=01       stop a project's Docker Compose services
PY ?= python3
P ?=
PROJECTS := $(if $(P),$(wildcard projects/$(P)*/),$(wildcard projects/*/))

.PHONY: test core-test project-tests arch-check eval check demo up down gateway-test

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

up:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml up -d --build --wait)

down:
	$(call for_compose_projects,docker compose -f $$d/docker-compose.yml down)

gateway-test:
	$(call for_compose_projects,docker build -q --target test -f $$d/gateway/Dockerfile -t ridgeline/mcp-gateway:test $$d && \
		docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL ridgeline/mcp-gateway:test)
