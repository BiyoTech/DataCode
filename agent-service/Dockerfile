# Agent service image (design 02, P7 cell stack).
#
# Build context is the datacode/ directory (the shared package is a local path
# dependency and must be present at install time):
#   docker build -f agent-service/Dockerfile -t datacode/agent:<ver> .
#
# Conversation orchestration and tool dispatch. Sessions live in the cell's
# own database; working copies and engine side effects belong to
# workspace-runtime, which is why git is NOT installed here.

FROM python:3.13-slim

WORKDIR /app

# build-essential keeps C-extension wheels installable when no prebuilt wheel
# matches the platform (grpcio in particular).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Shared package first (layer caching: it changes less often than the service).
COPY shared/python/ shared/python/
COPY agent-service/pyproject.toml agent-service/
# One pip command so the resolver satisfies `datacode-common` from the local
# directory candidate (never published to any index). The PostgreSQL driver
# (asyncpg) is a core dependency — same install as the dev venvs, so the two
# paths cannot drift apart again.
RUN pip install --no-cache-dir ./shared/python -e "./agent-service"

# Service source. conf/ is NOT baked in: container deployments mount a
# config directory via CONFIG_PATH (see deploy/) so one image serves every cell.
COPY agent-service/src/ agent-service/src/

WORKDIR /app/agent-service
USER app

# REST (8001) is the published surface; gRPC (50051) carries the streaming
# tool traffic and stays internal to the cell network.
EXPOSE 8001 50051

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=6 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8001/health', timeout=2)" || exit 1

CMD ["python", "-m", "src.main"]
