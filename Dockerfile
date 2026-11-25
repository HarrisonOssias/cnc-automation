# One Dockerfile, three images:  --target backend  |  --target web  (cloud)   --target agent  (edge, simulated Pi)

# 1. Build the React apps (pnpm)
FROM node:22-slim AS node
RUN npm install -g pnpm@12.8.1

FROM node AS kiosk
WORKDIR /kiosk
COPY source/edge/kiosk/package.json source/edge/kiosk/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY source/edge/kiosk/ ./
RUN pnpm build

FROM node AS web-build
WORKDIR /web
COPY source/cloud/web/package.json source/cloud/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY source/cloud/web/ ./
RUN pnpm build

# 2. Python source (pyproject maps forge_core, backend and agent to their folders under source/)
FROM python:3.12-slim AS python
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY source/shared/forge_core source/shared/forge_core
COPY source/cloud/backend source/cloud/backend
COPY source/edge/agent source/edge/agent

# 3a. Cloud: backend API
FROM python AS backend
RUN pip install --no-cache-dir ".[backend]"
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]

# 3b. Edge: Pi agent + built kiosk (simulates one machine's Pi)
FROM python AS agent
RUN pip install --no-cache-dir ".[agent]"
COPY --from=kiosk /kiosk/dist kiosk/dist
ENV AGENT_HOST=0.0.0.0 KIOSK_DIR=/app/kiosk/dist DATA_DIR=/data
EXPOSE 8080
CMD ["python", "-m", "agent", "serve"]

# 3c. Cloud: office web app, static files on their own server
FROM nginx:1.27-alpine AS web
COPY --from=web-build /web/dist /usr/share/nginx/html
