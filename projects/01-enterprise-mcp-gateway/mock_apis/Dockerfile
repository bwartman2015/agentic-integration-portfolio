# Ridgeline mock carrier + AR APIs (one image, two Compose services). Build context: the project root.
# Stages: base (pinned deps + code) -> test (adds tests) -> runtime (default, last).
# Reproducible: base image pinned by digest; every Python package pinned with hashes in requirements.lock
# (regenerate with ./lock-deps.sh mock_apis).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/mock_apis

WORKDIR /app

COPY mock_apis/requirements.lock mock_apis/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r mock_apis/requirements.lock

COPY mock_apis/ridgeline_mock_apis mock_apis/ridgeline_mock_apis
COPY mock_systems mock_systems

RUN useradd --system --uid 10001 --no-create-home mockapi

FROM base AS test
COPY mock_apis/tests mock_apis/tests
USER 10001
CMD ["python", "-m", "unittest", "discover", "-s", "mock_apis/tests", "-v"]

FROM base AS runtime
USER 10001
# The service is chosen by Compose: APP=carrier (port 8001) or APP=ar (port 8002).
ENV APP=carrier PORT=8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["sh", "-c", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT}/healthz', timeout=2).status == 200 else 1)\""]
CMD ["sh", "-c", "exec uvicorn ridgeline_mock_apis.${APP}:create_app --factory --host 0.0.0.0 --port ${PORT} --no-server-header"]
