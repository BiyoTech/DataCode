"""Ontology Service entry point — FastAPI REST API.

Provides a lightweight HTTP service that wraps the ontology core library
(ModelRegistry, SemanticSearchService, CrossRefValidator) as REST endpoints.

Does NOT require:
- Database connections
- SSE streaming
- Context Pool
- External dependencies beyond the YAML ontology files in the workspace.
"""

import uvicorn
from fastapi import FastAPI

from src.utils.logging import setup_logging, get_logger

setup_logging()
logger = get_logger(__name__)

app = FastAPI(
    title="Ontology Service",
    description="Semantic layer ontology operations — list, get, validate, search",
    version="0.2.0",
)

# Register route modules
from src.api.rest.ontology import router as ontology_router  # noqa: E402
app.include_router(ontology_router)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "ontology-service", "version": "0.2.0"}


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Default port 8003 — does not conflict with gateway (8000), agent (8001), sqlmesh (8002)
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
    )
