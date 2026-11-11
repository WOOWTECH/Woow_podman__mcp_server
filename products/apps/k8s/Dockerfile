# syntax=docker/dockerfile:1.7
# K8s MCP Admin + containers/kubernetes-mcp-server（Go, 原生 Streamable HTTP）— build context = monorepo ROOT
#   docker build -t woow-k8s-mcp-server -f apps/k8s/Dockerfile .
# Go binary 以 ADD --checksum 釘版釘 hash 烙進 image（runtime 零下載）。
# ⚠️ 勿改成 PyPI kubernetes-mcp-server 依賴——該 wheel 是首跑下載器（無 checksum、
#    cache 吃 $HOME），離線部署會炸。npm 平台包路線同禁。

# ── Stage 1: frontend (Vite, workspace-aware) ───────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/k8s/frontend ./apps/k8s/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/k8s/frontend

# ── Stage 2: python runtime ─────────────────────────────────────────────────
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.4 /uv /uvx /bin/

# 相依由 uv 管理；移除基底 pip／ensurepip 的舊 vendored certifi（GHSA-6q48-6c7w-9gw2）。
RUN python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.12/ensurepip

# kubernetes-mcp-server Go binary（GitHub release，BuildKit 建置期驗 sha256；
# 升版時 KMS_VERSION 與 checksum 必須成對更新，hash 不符 build 即 fail）
ARG KMS_VERSION=0.0.65
# Podman 4.9 Buildah does not implement remote ADD --checksum. Download into the
# image build, verify the same immutable PaaS digest, then grant execute access.
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/containers/kubernetes-mcp-server/releases/download/v${KMS_VERSION}/kubernetes-mcp-server-linux-amd64', '/usr/local/bin/kubernetes-mcp-server')" \
 && printf '%s  %s\n' '252ed5d0b7fe94837f01b982ff15ebd87b5dad67df5b5145ee30ec4f714fcfe2' \
    /usr/local/bin/kubernetes-mcp-server | sha256sum -c - \
 && chmod 755 /usr/local/bin/kubernetes-mcp-server
# 建置期驗證 binary 可執行（防 glibc/arch 失配出貨）
RUN kubernetes-mcp-server --version

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    MCP_ADMIN_CONFIG=/data/config.json

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/k8s/pyproject.toml ./apps/k8s/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-k8s-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/k8s/ ./apps/k8s/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-k8s-mcp-server

COPY --from=frontend-builder /app/apps/k8s/frontend/dist /app/static

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "k8s_mcp_admin.main:app"]
