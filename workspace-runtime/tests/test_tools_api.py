"""The agent tool surface — ``/rt/v1/tools/*`` (design 06 P3d).

agent-service holds no working copy: it reads, lists, greps and writes one
through these endpoints. So the guarantees its tools used to enforce locally are
now guarantees of THIS service, and they are asserted here against a real
directory tree.

Two families:

* the answers that are RESULTS rather than failures — missing file, binary file,
  no matches. A tool that turns them into a 500 tells the model the platform
  broke when the truth is "there is no such file";
* visibility — which entries a walk may see. The rules live in
  ``datacode_common.workspace_layout`` and are shared with the user's own file
  explorer: one working copy must not look different depending on who is
  looking at it.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.rest import tools as tools_api  # noqa: E402
from src.files import ops as file_ops  # noqa: E402
from src.files import search as file_search  # noqa: E402

_WORKSPACE_SEGMENTS = ("t1", "ws-t1-shop", "u1", "envs", "dev")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch) -> str:
    """A copy holding user content next to every kind of artefact."""
    base = tmp_path / "workspaces"
    root = base.joinpath(*_WORKSPACE_SEGMENTS)
    (root / "models").mkdir(parents=True)
    (root / "models" / "orders.sql").write_text("SELECT order_id\nFROM raw.orders;\n")
    (root / "models" / "users.sql").write_text("SELECT user_id FROM raw.users;\n")
    (root / "README.md").write_text("# shop\n")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary\x00payload")
    (root / ".agent" / "design").mkdir(parents=True)
    (root / ".agent" / "design" / "warehouse.md").write_text("# design\n")
    (root / ".agent" / "rules").mkdir(parents=True)
    (root / ".agent" / "rules" / "style.md").write_text("# rules\n")
    (root / ".agent" / "tasks").mkdir(parents=True)
    (root / ".agent" / "tasks" / "board.json").write_text("{}")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "thing.py").write_text("SELECT order_id\n")
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "ab").write_bytes(b"\x00")

    from src.config import settings

    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    return str(root)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    # The production error handlers, not a bare app: half of what these
    # endpoints promise is the STATUS a caller sees, and that is decided by the
    # handler rather than by the route.
    from src.main import _register_exception_handlers

    _register_exception_handlers(app)
    app.include_router(tools_api.router)
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, path: str, **payload) -> dict:
    response = client.post(f"/rt/v1/tools{path}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ── read: missing and binary are answers ─────────────────────────────────


class TestRead:
    def test_reads_text(self, client, workspace):
        body = _post(client, "/read", workspace_path=workspace, file_path="README.md")

        assert body["content"] == "# shop\n"
        assert body["missing"] is False
        assert body["binary"] is False

    def test_missing_file_is_a_result(self, client, workspace):
        body = _post(client, "/read", workspace_path=workspace, file_path="nope.sql")

        assert body["missing"] is True
        assert body["content"] == ""

    def test_binary_file_is_reported_not_decoded(self, client, workspace):
        """Decoding it would hand the model a screenful of replacement
        characters, or raise — neither of which says what is wrong."""
        body = _post(client, "/read", workspace_path=workspace, file_path="logo.png")

        assert body["binary"] is True
        assert body["content"] == ""

    def test_truncates_at_the_cap(self, client, workspace):
        body = _post(
            client,
            "/read",
            workspace_path=workspace,
            file_path="README.md",
            max_bytes=3,
        )

        assert body["truncated"] is True
        assert body["content"] == "# s"

    def test_escaping_the_workspace_is_refused(self, client, workspace):
        body = _post(
            client, "/read", workspace_path=workspace, file_path="../../../etc/passwd"
        )

        # The path never resolves inside the copy, so there is nothing to read.
        assert body["missing"] is True


class TestReadBytes:
    def test_returns_base64(self, client, workspace):
        body = _post(
            client, "/read-bytes", workspace_path=workspace, file_path="logo.png"
        )

        assert base64.b64decode(body["content_base64"]) == (
            b"\x89PNG\x00\x00binary\x00payload"
        )
        assert body["missing"] is False

    def test_missing_file_is_a_result(self, client, workspace):
        body = _post(
            client, "/read-bytes", workspace_path=workspace, file_path="nope.xlsx"
        )

        assert body["missing"] is True


# ── list: the visibility policy, shared with the explorer ────────────────


class TestList:
    def _paths(self, body: dict) -> set[str]:
        return {entry["path"] for entry in body["entries"]}

    def test_flat_listing_hides_only_the_never_listed_tier(self, client, workspace):
        """Browsing and indexing hide different things, deliberately.

        A flat listing is a file explorer: dot entries are shown (``.agent``,
        ``.gitignore``), and even a virtual environment stays browsable —
        hiding it would make the copy look different to the agent than to its
        owner. Only trees where a user decision is meaningless (Git's object
        store, byte-code, platform state) are never listed. The wider index
        tier applies when RECURSING, where those trees would drown the project.
        """
        paths = self._paths(_post(client, "/list", workspace_path=workspace))

        assert "models" in paths
        assert ".agent" in paths  # authored content stays visible
        assert ".venv" in paths  # browsable, just never indexed
        assert ".git" not in paths

    def test_recursive_listing_can_include_directories(self, client, workspace):
        body = _post(
            client,
            "/list",
            workspace_path=workspace,
            recursive=True,
            include_dirs=True,
        )
        paths = self._paths(body)

        assert "models" in paths
        assert "models/orders.sql" in paths
        # The agent's browser needs the shape of the tree; inferring directories
        # from file paths would miss the empty ones.
        assert any(entry["is_directory"] for entry in body["entries"])

    def test_recursive_listing_skips_dependency_trees(self, client, workspace):
        paths = self._paths(
            _post(client, "/list", workspace_path=workspace, recursive=True)
        )

        assert not [p for p in paths if p.startswith(".venv")]
        assert not [p for p in paths if p.startswith(".git/")]
        # Rewritten every turn and reachable through the todo tool: noise here.
        assert ".agent/tasks/board.json" not in paths
        assert ".agent/design/warehouse.md" in paths

    def test_a_workspace_below_the_platform_directory_stays_visible(
        self, tmp_path, client, monkeypatch
    ):
        """The dev default root is ``~/.datacode/workspaces/<tenant>/...``.

        Judging ABSOLUTE paths would match ``.datacode`` above the workspace and
        return an empty listing for every project on the machine.
        """
        base = tmp_path / ".datacode" / "workspaces"
        root = base.joinpath(*_WORKSPACE_SEGMENTS)
        (root / "models").mkdir(parents=True)
        (root / "models" / "orders.sql").write_text("SELECT 1;\n")

        from src.config import settings

        monkeypatch.setattr(settings, "workspace_base_path", str(base))

        paths = self._paths(
            _post(client, "/list", workspace_path=str(root), recursive=True)
        )
        assert "models/orders.sql" in paths


# ── search: the grep that used to run in agent-service ───────────────────


class TestSearch:
    def test_substring_search_is_case_insensitive(self, client, workspace):
        body = _post(
            client, "/search", workspace_path=workspace, query="order_id"
        )

        assert body["files"]["models/orders.sql"] == 1
        assert body["matches"][0]["line"] == 1

    def test_regex_and_context_lines(self, client, workspace):
        body = _post(
            client,
            "/search",
            workspace_path=workspace,
            query=r"FROM\s+raw\.orders",
            regex=True,
            context_before=1,
        )

        assert body["files"] == {"models/orders.sql": 1}
        assert any("L1:" in line for line in body["matches"][0]["context"])

    def test_glob_narrows_by_relative_path(self, client, workspace):
        body = _post(
            client,
            "/search",
            workspace_path=workspace,
            query="SELECT",
            pattern="models/**/*.sql",
        )

        assert set(body["files"]) == {"models/orders.sql", "models/users.sql"}

    def test_dependency_trees_and_binaries_are_skipped(self, client, workspace):
        body = _post(client, "/search", workspace_path=workspace, query="order_id")

        # .venv/lib/thing.py contains the needle and must not be searched.
        assert not [path for path in body["files"] if path.startswith(".venv")]

    def test_counting_modes_need_no_lines(self, client, workspace):
        body = _post(
            client,
            "/search",
            workspace_path=workspace,
            query="SELECT",
            include_matches=False,
        )

        assert body["files"]
        assert body["matches"] == []

    def test_truncation_is_reported(self, client, workspace):
        body = _post(
            client,
            "/search",
            workspace_path=workspace,
            query="SELECT",
            max_results=1,
        )

        assert body["truncated"] is True
        assert len(body["matches"]) == 1

    def test_invalid_regex_is_a_usage_error(self, client, workspace):
        """A 500 would read as "the platform broke"; the model can fix a 400."""
        response = client.post(
            "/rt/v1/tools/search",
            json={
                "workspace_path": workspace,
                "query": "(unclosed",
                "regex": True,
            },
        )

        assert response.status_code == 400
        assert "regular expression" in response.text

    def test_the_walk_is_pruned_not_filtered(self, client, workspace, monkeypatch):
        """Skipping a directory must skip its subtree — a dependency tree is
        most of the walk on a real working copy."""
        deep = Path(workspace) / ".venv" / "lib" / "site-packages" / "pkg"
        deep.mkdir(parents=True)
        (deep / "mod.py").write_text("SELECT 1\n")

        walked: list[str] = []
        real_walk = file_search.os.walk

        def _tracking_walk(top, *args, **kwargs):
            for entry in real_walk(top, *args, **kwargs):
                walked.append(str(entry[0]))
                yield entry

        monkeypatch.setattr(file_search.os, "walk", _tracking_walk)
        _post(client, "/search", workspace_path=workspace, query="SELECT")

        assert not [path for path in walked if ".venv" in path]


# ── write / delete ───────────────────────────────────────────────────────


class TestWriteAndDelete:
    def test_write_creates_parent_directories(self, client, workspace):
        _post(
            client,
            "/write",
            workspace_path=workspace,
            file_path="models/new/deep.sql",
            content="SELECT 2;\n",
        )

        assert (Path(workspace) / "models" / "new" / "deep.sql").read_text() == (
            "SELECT 2;\n"
        )

    def test_delete_reports_a_missing_path(self, client, workspace):
        body = _post(
            client, "/delete", workspace_path=workspace, file_path="models/gone.sql"
        )

        assert body["deleted"] is False


# ── the byte-level helpers the endpoints are built on ────────────────────


class TestBinaryHeuristic:
    def test_nul_bytes_are_binary(self):
        assert file_ops.looks_binary(b"text\x00more") is True

    def test_utf8_text_is_not(self):
        assert file_ops.looks_binary("SELECT 'héllo';\n".encode()) is False

    def test_empty_is_not(self):
        assert file_ops.looks_binary(b"") is False
