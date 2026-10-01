# syntax=docker/dockerfile:1
#
# fishsense-services-api. One image, two uses:
#   API (default):  uvicorn, runs as the unprivileged app role
#   migrations:     `fishsense-services-api migrate`, runs as the schema owner

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, in their own layer, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
COPY services/fishsense-services-api/pyproject.toml services/fishsense-services-api/
RUN uv sync --frozen --no-dev --package fishsense-services-api --no-install-workspace

COPY services/fishsense-services-api services/fishsense-services-api
RUN uv sync --frozen --no-dev --package fishsense-services-api --no-editable

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "--factory", "fishsense_services_api.main:create_app_from_env", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
