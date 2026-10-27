"""Knowledge metadata endpoint tests (design 06 P8 — route repatriation).

The endpoint replaced workspace-runtime's ``GET /documents/list``, so its
contract has two halves to pin:

* the RESPONSE SHAPE is byte-compatible with the old one — the frontend store
  must not learn which service answered;
* the workspace path is layout-checked (a malformed path is a 400, not a
  misleading empty list).

Runs against a throwaway SQLite database holding just the mirrored
``knowledge_documents`` table — the owner of that table (workspace-runtime)
creates it in production, so the test creates it the same way the runtime's
``create_all`` would.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.workspace.database as database
from src.api.rest import knowledge as knowledge_rest
from src.main import app

_BASE = "/workspaces"
_PATH = f"{_BASE}/t1/ws-t1-shop/alice/envs/dev"


@pytest.fixture()
async def knowledge_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)

    from sqlalchemy import insert

    engine = database.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(knowledge_rest.knowledge_documents.create)

    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": "sales.md",
            "workspace_path": _PATH,
            "title": "Sales metrics definitions",
            "filename": "sales.md",
            "content_type": "markdown",
            "file_size": 120,
            "tags": "sales,kpi",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "glossary.md",
            "workspace_path": _PATH,
            "title": "Glossary",
            "filename": "glossary.md",
            "content_type": "markdown",
            "file_size": 80,
            "tags": None,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "other.md",
            "workspace_path": f"{_BASE}/t2/ws-t2-shop/bob/envs/prod",
            "title": "Another tenant's document",
            "filename": "other.md",
            "content_type": "markdown",
            "file_size": 10,
            "tags": None,
            "created_at": now,
            "updated_at": now,
        },
    ]
    async with database.get_session() as session:
        for row in rows:
            await session.execute(
                insert(knowledge_rest.knowledge_documents).values(**row)
            )

    # The path validator resolves against the configured base; point it at
    # the same root the test paths live under (the property is env-backed).
    monkeypatch.setenv("CONTROL_SERVICE_WORKSPACE_BASE_PATH", _BASE)
    yield
    await database.close_db()


@pytest.fixture()
def client(knowledge_db):
    """HTTP client with the gateway-injected identity headers preset."""
    return TestClient(app, headers={"X-User-ID": "alice", "X-Tenant-ID": "t1"})


class TestKnowledgeList:
    def test_lists_only_the_requested_workspace(self, client):
        response = client.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": _PATH},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        names = {doc["id"] for doc in body["documents"]}
        assert names == {"sales.md", "glossary.md"}

    def test_response_shape_matches_the_runtime_contract(self, client):
        response = client.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": _PATH},
        )
        doc = next(d for d in response.json()["documents"] if d["id"] == "sales.md")
        assert set(doc) == {
            "id",
            "workspace_path",
            "title",
            "filename",
            "content_type",
            "file_size",
            "tags",
            "created_at",
            "updated_at",
        }
        assert doc["title"] == "Sales metrics definitions"
        assert doc["file_size"] == 120
        assert doc["tags"] == "sales,kpi"
        # ISO timestamps, not epoch floats: the frontend parses these.
        assert "T" in doc["created_at"]

    def test_search_filters_by_title_and_tags(self, client):
        response = client.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": _PATH, "search": "kpi"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["documents"][0]["id"] == "sales.md"

    def test_limit_bounds_fall_back_to_default(self, client):
        response = client.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": _PATH, "limit": 9999},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 2  # clamped, not an error

    def test_malformed_workspace_path_is_rejected(self, client):
        response = client.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": "/etc/passwd"},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "WORKSPACE_PATH_INVALID"

    def test_missing_identity_is_denied(self, client):
        api = TestClient(app)
        response = api.get(
            "/api/v1/knowledge/documents/list",
            params={"workspace_path": _PATH},
        )
        assert response.status_code == 403
