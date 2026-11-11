# Ridgeline CS copilot (Milestone 6). Build context: the project root (projects/01-enterprise-mcp-gateway/),
# plus agent-core as the named context `agentcore` (Compose `additional_contexts`; `docker build
# --build-context agentcore=../../agent-core`), so the repository root is never sent to the build.
# Stages: base (pinned deps + code) -> test (adds copilot/tests; `make copilot-test`) -> runtime (default).
# Reproducible: base image pinned by digest; every package pinned with hashes in requirements.lock
# (regenerate with ./lock-deps.sh copilot).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base
# Lets `make up` / `make ask` delete this project's superseded builds (dangling images).
LABEL org.ridgeline.project="ridgeline-portfolio"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY copilot/requirements.lock copilot/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r copilot/requirements.lock

COPY --from=agentcore agentcore agentcore
COPY cs_copilot cs_copilot
COPY copilot/fetch_model.py copilot/fetch_model.py
# The copilot's own copy of the policy data (scopes, write tools, restricted pattern), its knowledge base,
# runtime skills, scripted model plans (until Milestone 7) and the seeded mock systems (offline backend).
COPY policy/data.json policy/data.json
COPY knowledge knowledge
COPY skills skills
COPY fixtures/llm fixtures/llm
COPY evals/cases.json evals/cases.json
COPY mock_systems mock_systems

RUN useradd --system --uid 10001 --no-create-home copilot \
    # Mount point of the `models` volume: a new volume takes this owner, so `make models` can write to it.
    && mkdir /models && chown 10001 /models

FROM base AS test
COPY copilot/tests copilot/tests
USER 10001
CMD ["python", "-m", "unittest", "discover", "-s", "copilot/tests", "-v"]

FROM base AS runtime
USER 10001
ENTRYPOINT ["python", "-m", "cs_copilot.ask"]
