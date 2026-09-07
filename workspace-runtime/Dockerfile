# Data-plane workspace-runtime image (design 02 §2.5, design 06 P3/P4a).
#
# Build context is the datacode/ directory (the shared package is a local path
# dependency and must be present at install time):
#   docker build -f workspace-runtime/Dockerfile -t datacode/runtime:<ver> .
#
# The sole owner of working copies: SQLMesh engine + Ontology + Dagster code
# locations + Git + file IO + the L3-proc executor, in ONE process.

FROM python:3.13-slim

WORKDIR /app

# git is required for working-copy materialization (clone --bare -> worktree);
# build tools keep C-extension wheels installable when no prebuilt wheel
# matches the platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Shared package first (layer caching: it changes less often than the service).
COPY shared/python/ shared/python/
COPY workspace-runtime/pyproject.toml workspace-runtime/
# Install shared and the service in ONE pip command: the resolver then
# satisfies the service's `datacode-common` requirement from the local
# directory candidate (never published to any index). The PostgreSQL driver
# (asyncpg) is a core dependency — same install as the dev venvs, so the two
# paths cannot drift apart again.
#
# `[usercode]` is the set of packages user code may import from a code
# location, which runs on this interpreter. It is NOT an optional group here:
# an asset that imports pandas is not a special deployment, it is the ordinary
# case, and a promise that is only kept in the dev venv is not a promise.
RUN pip install --no-cache-dir ./shared/python -e "./workspace-runtime[usercode]"

# Service source. conf/ is NOT baked in: container deployments mount a
# config directory via CONFIG_PATH (see deploy/) so one image serves every
# environment.
COPY workspace-runtime/src/ workspace-runtime/src/

WORKDIR /app/workspace-runtime

# Working copies are materialized into the volume mounted at /data/workspaces
# (compose) or the per-replica PVC (K8s). Pre-create it so the runtime never
# needs to write to the image's filesystem.
RUN mkdir -p /data/workspaces && chown app:app /data/workspaces
USER app

# REST API (8002) is the only published surface: user traffic arrives via the
# gateway, lease placement via /internal/* from peers. Executor UDS sockets
# and code-location gRPC ports are pod-internal.
EXPOSE 8002

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8002/health', timeout=2)" || exit 1

CMD ["python", "-m", "src.main"]
