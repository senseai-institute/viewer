# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
#
# Build context: the repository root (Railway's default). packages/senseai-viewer/Dockerfile
# is the same build with the package directory as context; keep the two in step.
# 1. web:     build the web app with Node into server/senseai_viewer/static.
# 2. builder: resolve Python deps with uv into /opt/venv.
# 3. app:     slim runtime; uvicorn listens on $PORT. With no runs in $DATA_DIR the server
#             builds the synthetic demo run on first start (about 2 s).

ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS web
WORKDIR /app/web
COPY packages/senseai-viewer/web/package.json packages/senseai-viewer/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY packages/senseai-viewer/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY packages/senseai-viewer/pyproject.toml packages/senseai-viewer/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY packages/senseai-viewer/README.md packages/senseai-viewer/LICENSE packages/senseai-viewer/NOTICE ./
COPY packages/senseai-viewer/server ./server
COPY --from=web /app/server/senseai_viewer/static ./server/senseai_viewer/static
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    DATA_DIR=/data
COPY --from=builder /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 viewer && mkdir -p /data && chown viewer /data
USER viewer
WORKDIR /home/viewer
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn senseai_viewer.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
