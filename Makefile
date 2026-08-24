.PHONY: proto infra infra-down dev help

# Default target
help:
	@echo "DataCode - AI Native DataOps Platform"
	@echo ""
	@echo "Usage:"
	@echo "  make setup        - One-click dev environment setup (install all deps)"
	@echo "  make dev          - Start all services (frontend + backend)"
	@echo "  make dev-backend  - Start only backend services"
	@echo "  make dev-frontend - Start only frontend (Vite dev server)"
	@echo "  make stop         - Stop all services"
	@echo "  make proto        - Generate protobuf code (requires buf)"
	@echo "  make infra        - Start infrastructure (PostgreSQL, Redis, Gitea)"
	@echo "  make infra-down   - Stop infrastructure"
	@echo "  make lint         - Run linters for all services"
	@echo "  make test         - Run tests for all services"
	@echo "  make clean        - Clean generated files and build artifacts"

# Generate protobuf code for Go and Python
proto:
	@bash scripts/proto-gen.sh

# Detect docker compose command
DOCKER_COMPOSE := $(shell if docker compose version > /dev/null 2>&1; then echo "docker compose"; elif docker-compose version > /dev/null 2>&1; then echo "docker-compose"; else echo ""; fi)

# Start infrastructure services
infra:
	$(DOCKER_COMPOSE) -f deploy/docker-compose.infra.yml up -d

# Stop infrastructure services
infra-down:
	$(DOCKER_COMPOSE) -f deploy/docker-compose.infra.yml down

# Start all development services (frontend + backend)
dev:
	@bash scripts/dev-start.sh

# Start only backend services
dev-backend:
	@bash scripts/dev-backend.sh

# Start only frontend
dev-frontend:
	@bash scripts/dev-frontend.sh

# One-click dev environment setup (install all deps)
setup:
	@bash scripts/dev-setup.sh

# Stop all services
stop:
	@bash scripts/dev-stop.sh

# Run linters
lint:
	@echo "Running linters..."
	cd gateway && go vet ./...
	cd agent-service && ruff check src/ tests/
	cd sqlmesh-service && ruff check src/ tests/
	cd ontology-service && ruff check src/ tests/
	cd dagster-service && ruff check src/ tests/

# Run tests
test:
	@echo "Running tests..."
	cd gateway && go test ./...
	pip install -q -e shared/python
	cd agent-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q
	cd sqlmesh-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q
	cd ontology-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q
	cd dagster-service && pip install -q -e ".[dev]" && python -m pytest tests/ -q

# Clean generated files
clean:
	rm -rf gen/
	@echo "Cleaned generated files."
