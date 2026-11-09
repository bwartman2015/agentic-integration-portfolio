# Ridgeline MCP gateway. Build context: the project root (projects/01-enterprise-mcp-gateway/).
# Stages: base (pinned deps + code) -> test (adds tests; `make gateway-test`) -> runtime (default, last).
# Reproducible: base image pinned by digest; every Python package pinned with hashes in requirements.lock
# (regenerate: see gateway/README.md "Updating dependencies").
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/gateway

WORKDIR /app

# Dependencies first so code edits don't reinstall them.
COPY gateway/requirements.lock gateway/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r gateway/requirements.lock

COPY gateway/ridgeline_gateway gateway/ridgeline_gateway
# Seeded synthetic mock systems (the ERP behind get_sales_order until the Milestone 2 connectors).
COPY mock_systems mock_systems

RUN useradd --system --uid 10001 --no-create-home gateway
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_INTERNAL_PORT=8000

FROM base AS test
COPY gateway/tests gateway/tests
USER 10001
CMD ["python", "-m", "unittest", "discover", "-s", "gateway/tests", "-v"]

FROM base AS runtime
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "ridgeline_gateway.server"]
