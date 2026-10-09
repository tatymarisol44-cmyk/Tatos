# syntax=docker/dockerfile:1.7
# Multi-stage, reproducible build: base images are pinned by digest (Dependabot bumps them),
# dependencies come from uv.lock (--frozen), the runtime image has no build tools and
# runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder
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

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="agency-orchestrator" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
# ffmpeg renders the marketing videos (orchestrator.creatives): MP4 with H.264, as TikTok
# requires. It runs as a separate program, never linked into the Python process.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
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
