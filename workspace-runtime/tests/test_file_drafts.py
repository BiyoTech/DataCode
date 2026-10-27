"""Editor draft autosave tests (design 07 Q6: the <=5 s layer).

What is pinned here is the CONTRACT, not the CRUD plumbing:

* one draft per (working copy, file) — the 5-second debounce must not
  accumulate rows, and an overwrite must not resurrect a second one;
* a saved file has NO draft (delete on save is what keeps the recovery prompt
  from offering stale keystrokes after a real save);
* drafts are the OWNER's private typing state: another member of the same
  workspace gets 403, not a read.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.rest import files as files_api  # noqa: E402
from src.config import settings  # noqa: E402
from src.workspace import database as db_module  # noqa: E402
from src.workspace.models import FileDraftModel  # noqa: E402

_BASE = "base"
_COPY_PARTS = ("t1", "ws-t1-shop", "u1", "envs", "dev")
_USER = "u1"
_OTHER_USER = "u2"
_FILE = "models/orders.sql"


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    """A throwaway SQLite metadata DB, isolated from any configured engine."""
    monkeypatch.setenv("WORKSPACE_RUNTIME_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "WORKSPACE_RUNTIME_DB_SQLITE_PATH", str(tmp_path / "drafts.db")
    )
    # The engine is cached module-globally; force a rebuild against the
    # throwaway URL, and drop it again afterwards.
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    db_module.reset_engine()

    from src.workspace.database import init_db

    asyncio.run(init_db())
    yield

    asyncio.run(db_module.close_db())
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    db_module.reset_engine()


@pytest.fixture()
def workspace_path(tmp_path, monkeypatch):
    base = tmp_path / _BASE
    target = base.joinpath(*_COPY_PARTS)
    target.mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    return str(target)


@pytest.fixture()
def client(workspace_path):
    from fastapi.responses import JSONResponse

    from src.utils.errors import ServiceError

    app = FastAPI()
    app.include_router(files_api.router)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_response_dict()
        )

    with TestClient(app) as test_client:
        yield test_client


def _headers(user: str = _USER) -> dict:
    return {"X-User-ID": user, "X-Tenant-ID": "t1"}


# ── repository ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_is_one_row_per_file(sqlite_db, workspace_path):
    from src.workspace.repository import FileDraftRepository

    repo = FileDraftRepository()
    await repo.upsert(workspace_path, _FILE, "SELECT 1")
    await repo.upsert(workspace_path, _FILE, "SELECT 2")
    await repo.upsert(workspace_path, _FILE, "SELECT 3")

    from sqlalchemy import func, select

    from src.workspace.database import get_session

    async with get_session() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(FileDraftModel)
            )
        ).scalar_one()
    assert count == 1

    draft = await repo.get(workspace_path, _FILE)
    assert draft is not None
    assert draft.content == "SELECT 3"


@pytest.mark.asyncio
async def test_delete_then_get_is_none(sqlite_db, workspace_path):
    from src.workspace.repository import FileDraftRepository

    repo = FileDraftRepository()
    await repo.upsert(workspace_path, _FILE, "SELECT 1")

    assert await repo.delete(workspace_path, _FILE) is True
    assert await repo.get(workspace_path, _FILE) is None
    # Idempotent: a second delete reports nothing-to-do, not an error.
    assert await repo.delete(workspace_path, _FILE) is False


# ── endpoints ───────────────────────────────────────────────────────────────


def test_draft_roundtrip_and_delete(client, sqlite_db, workspace_path):
    # Save a draft (what the debounced editor does).
    response = client.put(
        "/api/v1/files/draft",
        json={
            "workspace_path": workspace_path,
            "file_path": _FILE,
            "content": "SELECT 1",
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["saved"] is True

    # Read it back (what opening a file does to check for recovery).
    response = client.get(
        "/api/v1/files/draft",
        params={"workspace_path": workspace_path, "file_path": _FILE},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["content"] == "SELECT 1"

    # Save the file for real → the draft must be gone.
    response = client.post(
        "/api/v1/files/draft/delete",
        json={"workspace_path": workspace_path, "file_path": _FILE},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    response = client.get(
        "/api/v1/files/draft",
        params={"workspace_path": workspace_path, "file_path": _FILE},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_draft_without_row_answers_404(client, sqlite_db, workspace_path):
    response = client.get(
        "/api/v1/files/draft",
        params={"workspace_path": workspace_path, "file_path": _FILE},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_drafts_are_restricted_to_the_copy_owner(
    client, sqlite_db, workspace_path
):
    put = client.put(
        "/api/v1/files/draft",
        json={
            "workspace_path": workspace_path,
            "file_path": _FILE,
            "content": "SELECT 1",
        },
        headers=_headers(),
    )
    assert put.status_code == 200

    # Another member of the same workspace may neither read nor overwrite.
    get = client.get(
        "/api/v1/files/draft",
        params={"workspace_path": workspace_path, "file_path": _FILE},
        headers=_headers(_OTHER_USER),
    )
    assert get.status_code == 403

    overwrite = client.put(
        "/api/v1/files/draft",
        json={
            "workspace_path": workspace_path,
            "file_path": _FILE,
            "content": "SELECT 999",
        },
        headers=_headers(_OTHER_USER),
    )
    assert overwrite.status_code == 403
