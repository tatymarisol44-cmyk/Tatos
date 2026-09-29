# syntax=docker/dockerfile:1.7
# Multi-stage, reproducible build: dependencies come from uv.lock (--frozen),
# the runtime image has no build tools and runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependency layer first so code changes don't invalidate it.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="agency-orchestrator" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# The agent catalog is baked in, so image tag == (code, catalog) version.
COPY vendor/agency-agents /app/catalog
ENV PATH=/app/.venv/bin:$PATH \
    AGENTS_DIR=/app/catalog \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "--factory", "orchestrator.api.app:app_factory", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
