# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
#
# 1. web:     build the frontend with Node.
# 2. builder: resolve Python deps with uv in the full Python image (it ships git, which the
#             senseai-ingest git dependency needs).
# 3. app:     slim runtime with the venv and the built frontend; uvicorn listens on $PORT.

ARG NODE_IMAGE=node:22-slim
ARG PYTHON_BUILD_IMAGE=python:3.12
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build   # -> /app/src/senseai_viewer/static

FROM ${PYTHON_BUILD_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
COPY --from=web /app/src/senseai_viewer/static ./src/senseai_viewer/static
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000
COPY --from=builder /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 viewer
USER viewer
WORKDIR /home/viewer
EXPOSE 8000
# Railway sets $PORT. The app refuses to start without VIEWER_PASSWORD when RAILWAY_ENVIRONMENT is set.
CMD ["sh", "-c", "exec uvicorn senseai_viewer.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
