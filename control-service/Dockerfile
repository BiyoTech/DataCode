# Control-plane control-service image (design 02, design 06 P5).
#
# Build context is the datacode/ directory (the shared package is a local path
# dependency and must be present at install time):
#   docker build -f control-service/Dockerfile -t datacode/control:<ver> .
#
# The control plane: environment registry + reconciler + TTL reaper, workspace
# metadata, Git credentials, quota bookkeeping. Stateless beyond the shared
# database — every filesystem/engine side effect is dispatched to
# workspace-runtime over the internal env-control channel.

FROM python:3.13-slim

WORKDIR /app

# build tools keep C-extension wheels installable when no prebuilt wheel
# matches the platform. git is deliberately absent: no working copies live in
# this container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Shared package first (layer caching: it changes less often than the service).
COPY shared/python/ shared/python/
COPY control-service/pyproject.toml control-service/
# Install shared and the service in ONE pip command: the resolver then
# satisfies the service's `datacode-common` requirement from the local
# directory candidate (never published to any index). The PostgreSQL drivers
# (asyncpg + psycopg2 for alembic) are core dependencies — same install as
# the dev venvs, so the two paths cannot drift apart again.
RUN pip install --no-cache-dir ./shared/python -e "./control-service"

# Service source. conf/ is NOT baked in: container deployments mount a
# config directory via CONFIG_PATH (see deploy/) so one image serves every
# environment.
COPY control-service/src/ control-service/src/
COPY control-service/alembic.ini control-service/alembic.ini
COPY control-service/alembic/ control-service/alembic/

WORKDIR /app/control-service
USER app

# Migrations are NOT run at boot: with more than one replica every one of them
# would race create_all + upgrade head against the others. Dev runs
# `make db-upgrade` (or lets dev-start.sh do it); production runs the migration
# Job (deploy/k8s/base/migrate-job.yaml) or the one-shot migrate service
# before the Deployments roll. AUTO_MIGRATE=1 remains available as an opt-in
# for single-replica evaluation installs.
ENV AUTO_MIGRATE=0

# REST API (8004) is the only published surface: user traffic arrives via the
# gateway, env-control dispatch leaves towards workspace-runtime.
EXPOSE 8004

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=6 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8004/health', timeout=2)" || exit 1

CMD ["python", "-m", "src.main"]
