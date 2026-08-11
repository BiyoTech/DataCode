"""Tests for workspace list reconciliation (disk as source of truth + DB).

Disk existence is authoritative; the DB supplements metadata and enables
recovery of workspaces whose local copy is absent (ephemeral disk / fresh Pod).
"""

from __future__ import annotations

import os

import pytest

from src.workspace import manager as mgr_module
from src.workspace.manager import WorkspaceManager, WorkspaceMetadata


def _make_ws_dir(base: str, user: str, name: str, dialect: str = "duckdb") -> str:
    path = os.path.join(base, user, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"project: {name}\nmodel_defaults:\n  dialect: {dialect}\n")
    return path


async def test_disk_only_workspace_is_listed_and_backfilled(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr_module.settings, "workspace_base_path", str(tmp_path))
    _make_ws_dir(str(tmp_path), "u1", "alpha", "duckdb")
    m = WorkspaceManager()

    persisted: list[str] = []

    async def _empty(_uid):
        return []

    async def _persist(ws):
        persisted.append(ws.workspace_id)

    monkeypatch.setattr(m, "_list_from_db", _empty)
    monkeypatch.setattr(m, "_persist_to_db", _persist)

    rows = await m.list_workspaces("u1")

    assert {w.name for w in rows} == {"alpha"}
    assert rows[0].dialect == "duckdb"
    # Disk-only workspace is back-filled into the DB for recoverability.
    assert "ws-u1-alpha" in persisted


async def test_db_only_workspace_is_listed_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr_module.settings, "workspace_base_path", str(tmp_path))
    m = WorkspaceManager()  # no disk dirs

    ghost = WorkspaceMetadata(
        workspace_id="ws-u1-remote",
        user_id="u1",
        name="remote",
        local_path=str(tmp_path / "u1" / "remote"),
        git_repo_url="https://example.com/r.git",
    )

    async def _db(_uid):
        return [ghost]

    monkeypatch.setattr(m, "_list_from_db", _db)

    rows = await m.list_workspaces("u1")

    # Present in DB but absent on disk → still listed (re-cloned lazily later).
    assert {w.name for w in rows} == {"remote"}


async def test_disk_and_db_merge_trusts_disk_for_path(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr_module.settings, "workspace_base_path", str(tmp_path))
    disk_path = _make_ws_dir(str(tmp_path), "u1", "beta", "mysql")
    m = WorkspaceManager()

    stale = WorkspaceMetadata(
        workspace_id="ws-u1-beta",
        user_id="u1",
        name="beta",
        local_path="/stale/path",
        dialect="duckdb",
        git_repo_url="https://example.com/b.git",
    )

    async def _db(_uid):
        return [stale]

    async def _persist(ws):
        pass

    monkeypatch.setattr(m, "_list_from_db", _db)
    monkeypatch.setattr(m, "_persist_to_db", _persist)

    rows = await m.list_workspaces("u1")

    assert len(rows) == 1
    w = rows[0]
    assert w.local_path == disk_path            # disk wins for local path
    assert w.git_repo_url == "https://example.com/b.git"  # DB metadata preserved
    assert w.dialect == "mysql"                 # disk dialect overrides default


async def test_db_unavailable_falls_back_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr_module.settings, "workspace_base_path", str(tmp_path))
    _make_ws_dir(str(tmp_path), "u1", "gamma", "duckdb")
    m = WorkspaceManager()

    async def _db_down(_uid):
        return None

    monkeypatch.setattr(m, "_list_from_db", _db_down)

    rows = await m.list_workspaces("u1")

    assert {w.name for w in rows} == {"gamma"}
