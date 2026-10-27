"""Quota endpoint tests — ``GET /api/v1/quotas`` over real HTTP.

Two things can only be verified at this layer, and both are security-relevant:

* an unauthenticated request is REFUSED rather than answered with somebody's
  quota — the endpoint carries no permission, so the identity header is the only
  thing standing between a caller and another tenant's commercial data;
* the tenant is taken from that header and nowhere else. A query parameter would
  turn the endpoint into an enumeration surface, so the test asks for a foreign
  tenant explicitly and asserts it is ignored.

The response SHAPE is pinned too, because the frontend reads it: ``current: null``
must survive serialization as null and not become 0.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.workspace.database as database
from src.config import settings
from src.main import app
from src.quota import get_quota_service
from src.workspace.models import WorkspaceModel


@pytest.fixture()
async def api(tmp_path, monkeypatch):
    """A client over the real app with a throwaway database.

    The real app matters: it carries the exception handlers that turn a
    ``PermissionDeniedError`` into 403 instead of a 500, which is precisely what
    the unauthenticated case asserts.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'quotas.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    await database.init_db()
    # No lifespan: startup would assert the database topology and launch the
    # reaper and the sweep, none of which this endpoint touches.
    yield TestClient(app)
    await database.close_db()


def _headers(tenant_id: str = "t1", user_id: str = "alice") -> dict:
    """The gateway-issued identity headers, which clients cannot forge."""
    return {"X-User-ID": user_id, "X-Tenant-ID": tenant_id}


async def _add_workspace(tenant_id: str, name: str) -> None:
    async with database.get_session() as session:
        session.add(
            WorkspaceModel(
                id=f"ws-{tenant_id}-{name}",
                tenant_id=tenant_id,
                owner_user_id="alice",
                name=name,
                status="active",
            )
        )


class TestAuthorization:
    async def test_request_without_identity_is_refused(self, api):
        """No permission guards this route, so the header is the whole boundary."""
        response = api.get("/api/v1/quotas")

        assert response.status_code == 403
        assert "quota" not in response.text.lower()

    async def test_tenant_comes_from_the_header_not_the_query(self, api):
        """A tenant id a client could name would be an enumeration surface."""
        await _add_workspace("t1", "mine")
        await _add_workspace("t2", "theirs")
        await _add_workspace("t2", "theirs-2")

        response = api.get(
            "/api/v1/quotas?tenant_id=t2", headers=_headers("t1")
        )

        assert response.status_code == 200
        body = response.json()
        assert body["tenant_id"] == "t1"
        workspace = next(
            item for item in body["resources"] if item["resource"] == "workspace"
        )
        # t1's own count, not t2's — the query parameter was ignored.
        assert workspace["current"] == 1


class TestResponseShape:
    async def test_reports_every_governed_resource(self, api, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 7)
        response = api.get("/api/v1/quotas", headers=_headers())

        assert response.status_code == 200
        body = response.json()
        assert {item["resource"] for item in body["resources"]} == {
            "workspace",
            "environment",
            "concurrent_leases",
            "runtime_replicas",
        }
        workspace = next(
            item for item in body["resources"] if item["resource"] == "workspace"
        )
        assert workspace["limit"] == 7
        assert workspace["current"] == 0
        assert workspace["exhausted"] is False

    async def test_unknown_usage_serializes_as_null_not_zero(self, api):
        """The frontend must be able to render "unknown" distinctly.

        Replica occupancy has no honest instantaneous reading, and lease usage is
        unknown until the capacity loop reports it; both must arrive as null so
        the UI does not claim a measurement that was never made.
        """
        response = api.get("/api/v1/quotas", headers=_headers())

        by_resource = {
            item["resource"]: item for item in response.json()["resources"]
        }
        assert by_resource["runtime_replicas"]["current"] is None
        assert by_resource["concurrent_leases"]["current"] is None

    async def test_retry_after_matches_the_configured_value(self, api, monkeypatch):
        """The UI's retry hint must use the same number the 429 header carries."""
        monkeypatch.setattr(settings, "quota_retry_after_seconds", 123)

        response = api.get("/api/v1/quotas", headers=_headers())

        assert response.json()["retry_after_seconds"] == 123

    async def test_exhausted_resource_is_flagged(self, api, monkeypatch):
        monkeypatch.setattr(settings, "quota_default_max_workspaces", 1)
        await _add_workspace("t1", "only")

        response = api.get("/api/v1/quotas", headers=_headers())

        workspace = next(
            item
            for item in response.json()["resources"]
            if item["resource"] == "workspace"
        )
        assert workspace["exhausted"] is True

    async def test_lease_usage_is_reported_once_observed(self, api):
        await get_quota_service().apply_lease_usage({"t1": 4})

        response = api.get("/api/v1/quotas", headers=_headers())

        body = response.json()
        leases = next(
            item
            for item in body["resources"]
            if item["resource"] == "concurrent_leases"
        )
        assert leases["current"] == 4
        assert body["peak_concurrent_leases"] == 4
