"""The file tools, exercised against a stand-in workspace-runtime.

These tools no longer have a filesystem: they describe an operation, the runtime
performs it, and they render the answer. So what is asserted here is exactly
that contract — WHAT is requested of the runtime, and what the model is told
about the response, including the three answers that are results rather than
failures (missing, binary, unavailable).

The stand-in replaces the façade's transport, not the façade: path validation,
the missing/binary semantics and the error translation are part of what the
tools rely on and must run for real.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agents.tool_context import ToolContext as SdkToolContext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.context.tool_context import ToolContext  # noqa: E402
from src.tools import apply_patch as apply_patch_mod  # noqa: E402
from src.tools import create_file as create_file_mod  # noqa: E402
from src.tools import list_dir as list_dir_mod  # noqa: E402
from src.tools import read_file as read_file_mod  # noqa: E402
from src.tools import search_code as search_code_mod  # noqa: E402
from src.tools import workspace_fs  # noqa: E402
from src.tools.runtime_client import RuntimeUnavailable  # noqa: E402

WORKSPACE = "/data/workspaces/t1/ws-t1-shop/u1/envs/dev"


class FakeRuntime:
    """A workspace-runtime that keeps its working copy in a dict."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.binary: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_with: str | None = None

    # ── the surface RuntimeClient exposes ──────────────────────────────
    @property
    def configured(self) -> bool:
        return True

    def _record(self, name: str, **kwargs) -> None:
        if self.fail_with:
            raise RuntimeUnavailable(self.fail_with)
        self.calls.append((name, kwargs))

    async def read(self, workspace_path: str, file_path: str) -> dict:
        self._record("read", workspace_path=workspace_path, file_path=file_path)
        if file_path in self.binary:
            return {"content": "", "missing": False, "binary": True}
        if file_path not in self.files:
            return {"content": "", "missing": True, "binary": False}
        return {"content": self.files[file_path], "missing": False, "binary": False}

    async def read_binary(
        self, workspace_path: str, file_path: str, *, max_bytes: int
    ) -> dict:
        import base64

        self._record("read_binary", file_path=file_path, max_bytes=max_bytes)
        data = self.binary.get(file_path)
        if data is None:
            return {"content_base64": "", "missing": True}
        return {
            "content_base64": base64.b64encode(data).decode("ascii"),
            "missing": False,
        }

    async def write(self, workspace_path: str, file_path: str, content: str) -> dict:
        self._record("write", file_path=file_path, content=content)
        self.files[file_path] = content
        return {"path": file_path}

    async def delete(self, workspace_path: str, file_path: str) -> dict:
        self._record("delete", file_path=file_path)
        return {"deleted": self.files.pop(file_path, None) is not None}

    async def list_dir(
        self,
        workspace_path: str,
        path: str = "",
        pattern: str = "*",
        recursive: bool = False,
        include_dirs: bool = False,
    ) -> dict:
        self._record(
            "list",
            path=path,
            pattern=pattern,
            recursive=recursive,
            include_dirs=include_dirs,
        )
        entries = [
            {"path": name, "is_directory": False, "size_bytes": len(body)}
            for name, body in sorted(self.files.items())
        ]
        return {"entries": entries}

    async def search(self, workspace_path: str, query: str, **kwargs) -> dict:
        self._record("search", query=query, **kwargs)
        files: dict[str, int] = {}
        matches = []
        for name, body in sorted(self.files.items()):
            for number, line in enumerate(body.splitlines(), start=1):
                if query.lower() in line.lower():
                    files[name] = files.get(name, 0) + 1
                    if kwargs.get("include_matches", True):
                        matches.append(
                            {"path": name, "line": number, "text": line, "context": []}
                        )
        return {"matches": matches, "files": files, "truncated": False}


@pytest.fixture()
def runtime(monkeypatch) -> FakeRuntime:
    fake = FakeRuntime(
        {
            "models/orders.sql": "SELECT order_id\nFROM raw.orders;\n",
            "README.md": "# shop\n",
        }
    )
    monkeypatch.setattr(workspace_fs, "get_runtime_client", lambda: fake)
    return fake


def _ctx(tool_name: str, arguments: str) -> "SdkToolContext":
    """The context object the SDK hands a tool at invocation time.

    The SDK's own ToolContext, not a bare RunContextWrapper: ``on_invoke_tool``
    reads ``tool_name``, ``tool_arguments`` and ``run_config`` off it, so a
    lookalike would test the wrapper rather than the tool.
    """
    return SdkToolContext(
        context=ToolContext(workspace_path=WORKSPACE, user_id="u1", session_id="s1"),
        tool_name=tool_name,
        tool_call_id=f"call-{tool_name}",
        tool_arguments=arguments,
    )


async def _invoke(tool, **kwargs) -> str:
    """Call a registered SDK tool the way the agent runtime does."""
    import json

    arguments = json.dumps(kwargs)
    return await tool.on_invoke_tool(_ctx(tool.name, arguments), arguments)


# ── read_file ────────────────────────────────────────────────────────────


class TestReadFile:
    async def test_renders_a_numbered_window(self, runtime):
        out = await _invoke(read_file_mod.read_file, file_path="models/orders.sql")

        assert "[Lines 1-2 / 2 total]" in out
        assert "     1: SELECT order_id" in out
        assert runtime.calls[0][0] == "read"

    async def test_missing_file_is_reported_as_such(self, runtime):
        out = await _invoke(read_file_mod.read_file, file_path="nope.sql")

        assert "File not found: nope.sql" in out

    async def test_binary_file_is_reported_not_dumped(self, runtime):
        runtime.binary["logo.png"] = b"\x89PNG\x00"

        out = await _invoke(read_file_mod.read_file, file_path="logo.png")

        assert "binary" in out.lower()

    async def test_an_office_document_is_fetched_as_bytes_and_parsed(
        self, runtime, monkeypatch
    ):
        """Office parsing stays here (the parsers are LLM-facing), but the
        bytes come over the wire and are staged in this process's own tmp."""
        runtime.binary["docs/report.xlsx"] = b"PK\x03\x04fake-xlsx"
        staged: dict[str, str] = {}

        def _fake_parser(path, sheet_name, max_rows, mode):
            staged["path"] = path
            assert Path(path).read_bytes() == b"PK\x03\x04fake-xlsx"
            return "| a | b |\n| 1 | 2 |", "markitdown"

        monkeypatch.setattr(read_file_mod, "parse_office_document", _fake_parser)

        out = await _invoke(read_file_mod.read_file, file_path="docs/report.xlsx")

        assert "| a | b |" in out
        assert WORKSPACE not in staged["path"], "staged inside the working copy"
        assert not Path(staged["path"]).exists(), "staging file outlived the parse"

    async def test_an_absolute_path_is_refused_before_a_round_trip(self, runtime):
        out = await _invoke(read_file_mod.read_file, file_path="/etc/passwd")

        assert "relative" in out
        assert runtime.calls == []

    async def test_an_unreachable_runtime_is_an_error_not_an_empty_file(self, runtime):
        runtime.fail_with = "cannot reach workspace-runtime"

        out = await _invoke(read_file_mod.read_file, file_path="README.md")

        assert out.startswith("Error reading file")


# ── create_file ──────────────────────────────────────────────────────────


class TestCreateFile:
    async def test_writes_through_the_runtime(self, runtime):
        out = await _invoke(
            create_file_mod.create_file,
            file_path="models/new.sql",
            content="SELECT 1;\n",
        )

        assert "created successfully" in out
        assert runtime.files["models/new.sql"] == "SELECT 1;\n"

    async def test_a_failed_write_is_not_reported_as_success(self, runtime):
        runtime.fail_with = "lease not resolvable"

        out = await _invoke(
            create_file_mod.create_file, file_path="models/new.sql", content="x"
        )

        assert out.startswith("Error")


# ── list_dir ─────────────────────────────────────────────────────────────


class TestListDir:
    async def test_lists_and_filters_by_pattern(self, runtime):
        out = await _invoke(list_dir_mod.list_dir, path=".", pattern="*.sql")

        assert "models/orders.sql" in out
        assert "README.md" not in out

    async def test_asks_for_directories_so_the_tree_has_shape(self, runtime):
        await _invoke(list_dir_mod.list_dir, path=".", recursive=True)

        _, kwargs = runtime.calls[0]
        assert kwargs["include_dirs"] is True
        assert kwargs["recursive"] is True

    async def test_the_name_filter_is_applied_here_not_upstream(self, runtime):
        """The runtime matches a pattern against file NAMES, which would hide a
        directory whose children match. The tool asks for everything and
        filters, so ``*.sql`` still shows the folders that contain them."""
        await _invoke(list_dir_mod.list_dir, path=".", pattern="*.sql")

        _, kwargs = runtime.calls[0]
        assert kwargs["pattern"] == "*"


# ── search_code ──────────────────────────────────────────────────────────


class TestSearchCode:
    async def test_content_mode_renders_matches(self, runtime):
        out = await _invoke(search_code_mod.search_code, query="order_id")

        assert "models/orders.sql" in out
        assert "L1:" in out

    async def test_counting_modes_do_not_ask_for_lines(self, runtime):
        out = await _invoke(
            search_code_mod.search_code, query="SELECT", output_mode="count"
        )

        _, kwargs = runtime.calls[0]
        assert kwargs["include_matches"] is False
        assert "Match counts" in out

    async def test_no_matches_is_a_sentence_not_an_error(self, runtime):
        out = await _invoke(search_code_mod.search_code, query="zzz-not-there")

        assert out.startswith("No matches found")

    async def test_regex_and_context_reach_the_runtime(self, runtime):
        await _invoke(
            search_code_mod.search_code,
            query="order_.*",
            regex=True,
            context_before=2,
            context_after=3,
        )

        _, kwargs = runtime.calls[0]
        assert kwargs["regex"] is True
        assert (kwargs["context_before"], kwargs["context_after"]) == (2, 3)


# ── apply_patch ──────────────────────────────────────────────────────────


class TestApplyPatch:
    @pytest.fixture(autouse=True)
    def _silence_timeline(self, monkeypatch):
        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(apply_patch_mod.event_bus, "publish", _noop)
        monkeypatch.setattr(
            apply_patch_mod.session_manager, "add_timeline_event_async", _noop
        )

    async def test_update_reads_then_writes_through_the_runtime(self, runtime):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: models/orders.sql\n"
            "@@\n"
            "-SELECT order_id\n"
            "+SELECT order_id, status\n"
            " FROM raw.orders;\n"
            "*** End Patch\n"
        )

        out = await _invoke(apply_patch_mod.apply_patch, patch=patch)

        assert "Updated models/orders.sql" in out
        assert "status" in runtime.files["models/orders.sql"]
        assert [name for name, _ in runtime.calls] == ["read", "write"]

    async def test_a_missing_file_is_rejected_before_anything_is_written(
        self, runtime
    ):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: models/ghost.sql\n"
            "@@\n"
            "-a\n"
            "+b\n"
            "*** End Patch\n"
        )

        out = await _invoke(apply_patch_mod.apply_patch, patch=patch)

        assert "file does not exist" in out
        assert "write" not in [name for name, _ in runtime.calls]

    async def test_a_rename_writes_the_target_and_deletes_the_source(self, runtime):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: README.md\n"
            "*** Move to: docs/README.md\n"
            "@@\n"
            "-# shop\n"
            "+# shop analytics\n"
            "*** End Patch\n"
        )

        await _invoke(apply_patch_mod.apply_patch, patch=patch)

        assert "docs/README.md" in runtime.files
        assert "README.md" not in runtime.files

    async def test_nothing_is_written_when_one_operation_cannot_resolve(
        self, runtime
    ):
        """Phase 1 resolves everything before phase 2 commits anything."""
        patch = (
            "*** Begin Patch\n"
            "*** Add File: models/new.sql\n"
            "+SELECT 1;\n"
            "*** Update File: models/ghost.sql\n"
            "@@\n"
            "-a\n"
            "+b\n"
            "*** End Patch\n"
        )

        out = await _invoke(apply_patch_mod.apply_patch, patch=patch)

        assert out.startswith("Error")
        assert "models/new.sql" not in runtime.files
