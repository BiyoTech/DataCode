"""A code location whose Python fails to import must be reported, not swallowed.

The defect these tests pin is a query that did not ask the question. Dagster
answers a failed load with a COMPLETED entry: ``loadStatus`` is ``LOADED`` and
the traceback sits beside it in ``locationOrLoadError``. ``reloadRepositoryLocation``
— the primary path, taken for every location the webserver already knows —
requested only ``name loadStatus``, so a code location serving no assets at all
was reported as reloaded fine.

That is how a missing third-party module in user code (``ModuleNotFoundError: No
module named 'pyspark'`` in a file under ``defs/``) reaches the user as "the
asset DAG is empty" with nothing anywhere naming the cause: ``defs/`` is
auto-discovered as a whole, so one bad import takes the location's ENTIRE asset
set down, platform bridge assets included.

The message is the user code's own traceback, which no other layer attributes:
the gRPC server's stderr lands in this service's console unlabelled, and the
REST layer only knows whether a process is alive.
"""

from __future__ import annotations

from src.codeloc import client as client_module
from src.codeloc.client import (
    _RELOAD_LOCATION_MUTATION,
    _RELOAD_WORKSPACE_MUTATION,
    DagsterGraphQLClient,
)

TRACEBACK = "ModuleNotFoundError: No module named 'pyspark'"


class _Recorder:
    """Stands in for the module logger, capturing every structured call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, event: str, **kw):
        self.events.append((event, kw))

    def info(self, event, **kw):
        self._record(event, **kw)

    def warning(self, event, **kw):
        self._record(event, **kw)

    def error(self, event, **kw):
        self._record(event, **kw)


def _client(monkeypatch, response: dict) -> tuple[DagsterGraphQLClient, _Recorder]:
    recorder = _Recorder()
    monkeypatch.setattr(client_module, "logger", recorder)
    dagster = DagsterGraphQLClient()

    async def fake_execute(query, variables=None):
        return response

    monkeypatch.setattr(dagster, "execute", fake_execute)
    return dagster, recorder


class TestReloadLocationReportsTheLoadError:
    async def test_a_failed_load_is_logged(self, monkeypatch):
        """The gap: this path never inspected the error it was handed."""
        dagster, recorder = _client(
            monkeypatch,
            {
                "reloadRepositoryLocation": {
                    "__typename": "WorkspaceLocationEntry",
                    "name": "ws-t1-abc-dev",
                    "loadStatus": "LOADED",
                    "locationOrLoadError": {
                        "__typename": "PythonError",
                        "message": TRACEBACK,
                    },
                }
            },
        )

        await dagster.reload_code_location("ws-t1-abc-dev")

        reported = [kw for event, kw in recorder.events if event == "code_location_load_error"]
        assert len(reported) == 1
        assert reported[0]["location"] == "ws-t1-abc-dev"
        assert TRACEBACK in reported[0]["error"]

    async def test_a_healthy_location_logs_nothing(self, monkeypatch):
        """No false positives: most reloads succeed and must stay quiet."""
        dagster, recorder = _client(
            monkeypatch,
            {
                "reloadRepositoryLocation": {
                    "__typename": "WorkspaceLocationEntry",
                    "name": "ws-t1-abc-dev",
                    "loadStatus": "LOADED",
                    "locationOrLoadError": {"__typename": "RepositoryLocation"},
                }
            },
        )

        result = await dagster.reload_code_location("ws-t1-abc-dev")

        assert result["load_status"] == "LOADED"
        assert recorder.events == []

    async def test_the_response_shape_is_unchanged(self, monkeypatch):
        """Reporting an error must not disturb what callers already read."""
        dagster, _ = _client(
            monkeypatch,
            {
                "reloadRepositoryLocation": {
                    "__typename": "WorkspaceLocationEntry",
                    "name": "ws-t1-abc-dev",
                    "loadStatus": "LOADED",
                    "locationOrLoadError": {"__typename": "PythonError", "message": TRACEBACK},
                }
            },
        )

        result = await dagster.reload_code_location("ws-t1-abc-dev")

        assert result == {"name": "ws-t1-abc-dev", "load_status": "LOADED"}

    def test_the_mutation_asks_for_the_error(self):
        """Structural: the log can only fire if the query requests the field.

        The behavioural tests stub ``execute``, so they would keep passing if
        the selection set dropped ``locationOrLoadError`` — the server would
        simply stop returning it and the error would go unreported again.
        """
        assert "locationOrLoadError" in _RELOAD_LOCATION_MUTATION
        assert "locationOrLoadError" in _RELOAD_WORKSPACE_MUTATION


class TestBothReloadPathsShareOneReport:
    async def test_reload_workspace_reports_every_broken_entry(self, monkeypatch):
        dagster, recorder = _client(
            monkeypatch,
            {
                "reloadWorkspace": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "broken",
                            "loadStatus": "LOADED",
                            "locationOrLoadError": {
                                "__typename": "PythonError",
                                "message": TRACEBACK,
                            },
                        },
                        {
                            "name": "healthy",
                            "loadStatus": "LOADED",
                            "locationOrLoadError": {"__typename": "RepositoryLocation"},
                        },
                    ],
                }
            },
        )

        result = await dagster.reload_workspace()

        assert [e["name"] for e in result["locations"]] == ["broken", "healthy"]
        reported = [kw["location"] for event, kw in recorder.events
                    if event == "code_location_load_error"]
        # One broken entry is named; the healthy one is not dragged in with it.
        assert reported == ["broken"]
