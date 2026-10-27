FROM python:3.11-slim

WORKDIR /app

# Shared package is a local path dependency, so it must be present at install
# time. Build context is the datacode/ directory.
COPY shared/python/ shared/python/
COPY deploy/orchestrator/pyproject.toml deploy/orchestrator/
COPY deploy/orchestrator/src/ deploy/orchestrator/src/

# Install shared and the service in ONE pip command: the resolver then
# satisfies the service's `datacode-common` requirement from the local
# directory candidate (never published to any index).
RUN pip install --no-cache-dir ./shared/python ./deploy/orchestrator

# No ports: the syncer only makes outbound calls (data plane REST, webserver
# GraphQL) and writes the shared workspace file.
CMD ["datacode-workspace-sync"]
