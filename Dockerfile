# syntax=docker/dockerfile:1
#
# Two images from one workspace:
#
#   api (default, the last stage):
#     API:         uvicorn, runs as the unprivileged app role
#     migrations:  `fishsense-services-api migrate`, runs as the schema owner
#   orchestrator (`--target orchestrator`):
#     the Temporal worker, also as the app role
#   processor (`--target processor`):
#     the compute worker; no database or NAS access, only Temporal
#
# amd64 only for now: the orchestrator's synology-filestation wheel is built for
# manylinux x86_64. The processor is the part that goes ARM64 later (PLAN §3).

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.13-slim AS build-base
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/fishsense-services-api/pyproject.toml services/fishsense-services-api/
COPY services/fishsense-services-contracts/pyproject.toml services/fishsense-services-contracts/
COPY services/fishsense-services-orchestrator/pyproject.toml services/fishsense-services-orchestrator/
COPY services/fishsense-services-processor/pyproject.toml services/fishsense-services-processor/

# Dependencies first, in their own layer, so code changes don't reinstall them.
FROM build-base AS build-api
RUN uv sync --frozen --no-dev --package fishsense-services-api --no-install-workspace
COPY services/fishsense-services-api services/fishsense-services-api
RUN uv sync --frozen --no-dev --package fishsense-services-api --no-editable

FROM build-base AS build-orchestrator
RUN uv sync --frozen --no-dev --package fishsense-services-orchestrator --no-install-workspace
# The orchestrator depends on the API package (its database side) and the
# processing contract.
COPY services/fishsense-services-api services/fishsense-services-api
COPY services/fishsense-services-contracts services/fishsense-services-contracts
COPY services/fishsense-services-orchestrator services/fishsense-services-orchestrator
RUN uv sync --frozen --no-dev --package fishsense-services-orchestrator --no-editable

FROM build-base AS build-processor
RUN uv sync --frozen --no-dev --package fishsense-services-processor --no-install-workspace
COPY services/fishsense-services-contracts services/fishsense-services-contracts
COPY services/fishsense-services-processor services/fishsense-services-processor
RUN uv sync --frozen --no-dev --package fishsense-services-processor --no-editable

FROM python:3.13-slim AS runtime
RUN useradd --system --uid 10001 --no-create-home app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

FROM runtime AS orchestrator
COPY --from=build-orchestrator /app/.venv /app/.venv
USER app
CMD ["fishsense-services-orchestrator"]

FROM runtime AS processor
COPY --from=build-processor /app/.venv /app/.venv
USER app
CMD ["fishsense-services-processor"]

FROM runtime AS api
COPY --from=build-api /app/.venv /app/.venv
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "--factory", "fishsense_services_api.main:create_app_from_env", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
