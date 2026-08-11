FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (git for code location loading, build tools for daft)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy source code
COPY src/ src/

# REST API (8004), dagster-webserver (3001, local mode), code location gRPC (4000)
EXPOSE 8004 3001 4000

CMD ["python", "-m", "src.main"]
