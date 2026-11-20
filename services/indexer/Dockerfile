# syntax=docker/dockerfile:1.7
# ScoutOSS indexer — build context is the repository root.
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM python:3.12.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY services/common/pyproject.toml services/common/
COPY services/indexer/pyproject.toml services/indexer/
COPY services/copilot/pyproject.toml services/copilot/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package scoutoss-indexer --no-install-workspace
COPY services/common services/common
COPY services/indexer services/indexer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package scoutoss-indexer --no-editable

FROM python:3.12.12-slim
LABEL org.opencontainers.image.source="https://github.com/scoutoss/scoutoss" \
      org.opencontainers.image.title="scoutoss-indexer" \
      org.opencontainers.image.licenses="Apache-2.0"
# Pick up Debian security fixes released after the pinned base image (CI Trivy gate).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 10001 scout && useradd -u 10001 -g scout -r -s /usr/sbin/nologin scout
COPY --from=build /app/.venv /app/.venv
RUN mkdir -p /spool /data && chown 10001:10001 /spool /data
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
WORKDIR /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health',timeout=3)"]
CMD ["scoutoss-indexer", "serve"]
