"""Cell self-registration tests (design 06 P7).

Two properties matter, and they pull in opposite directions:

* a cell MUST publish its endpoint, or the global edge has no address to send a
  misdirected client to — and the failure is silent for existing tenants;
* registration MUST fail open, because a cell whose control plane cannot reach
  the global tier still has to serve the tenants already placed on it. Refusing
  to start would turn a global outage into a per-cell one.
"""

from __future__ import annotations

import json

import httpx
import pytest

from datacode_common.identity.cell_registry import (
    ENV_CELL_ENDPOINT,
    ENV_CELL_REGION,
    CellRegistration,
    register_cell,
    registration_from_env,
)


class TestRegistrationFromEnv:
    def test_no_endpoint_means_no_registration(self, monkeypatch):
        """The normal single-cell state: with one cell there is nothing to route
        between, so demanding an endpoint would burden every dev machine."""
        monkeypatch.delenv(ENV_CELL_ENDPOINT, raising=False)

        assert registration_from_env() is None

    def test_a_blank_endpoint_is_treated_as_absent(self, monkeypatch):
        """A deployment template that renders an empty value must not register a
        cell with no address — the edge would resolve it and have nowhere to go."""
        monkeypatch.setenv(ENV_CELL_ENDPOINT, "   ")

        assert registration_from_env() is None

    def test_it_reads_the_endpoint_region_and_cell_id(self, monkeypatch):
        monkeypatch.setenv("DATACODE_CELL_ID", "cell-7")
        monkeypatch.setenv(ENV_CELL_ENDPOINT, "https://cell-7.example ")
        monkeypatch.setenv(ENV_CELL_REGION, "eu-west")

        registration = registration_from_env(capacity_tenants=150)

        assert registration is not None
        assert registration.cell_id == "cell-7"
        assert registration.endpoint == "https://cell-7.example"
        assert registration.region == "eu-west"
        assert registration.capacity_tenants == 150

    def test_capacity_comes_from_the_caller_not_the_environment(self, monkeypatch):
        """Capacity is a control-plane setting, so it arrives through the service
        config rather than a second env var that could disagree with it."""
        monkeypatch.setenv(ENV_CELL_ENDPOINT, "https://c.example")

        assert registration_from_env(capacity_tenants=42).capacity_tenants == 42


class TestPayload:
    def test_it_never_reports_assigned_tenants(self):
        """Derived on the global side from the placement table: a cell stating
        its own tenant count would let a stale value distort placement."""
        payload = CellRegistration(
            cell_id="cell-1", endpoint="https://c.example"
        ).to_payload()

        assert "assigned_tenants" not in payload

    def test_status_defaults_to_active(self):
        """PickCell skips a cell whose status is not active, so an unset status
        would make a freshly registered cell invisible to placement."""
        payload = CellRegistration(
            cell_id="cell-1", endpoint="https://c.example"
        ).to_payload()

        assert payload["status"] == "active"


class TestRegisterCell:
    @pytest.fixture
    def registration(self):
        return CellRegistration(
            cell_id="cell-1", endpoint="https://cell-1.example", capacity_tenants=200
        )

    @staticmethod
    def _mock_transport(monkeypatch, handler) -> None:
        """Route every AsyncClient the production code builds through the
        mock transport.

        The ORIGINAL class must be captured before patching: resolving
        ``httpx.AsyncClient`` inside the lambda would find the patch itself
        and recurse (the exact bug this helper replaced).
        """
        original = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
        )

    async def test_it_posts_to_the_global_registry(
        self, registration, monkeypatch
    ):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("X-Internal-Token")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"cell_id": "cell-1"})

        self._mock_transport(monkeypatch, handler)

        accepted = await register_cell(
            "http://identity:8003/", registration, token="secret"
        )

        assert accepted is True
        assert seen["url"] == "http://identity:8003/internal/cells"
        assert seen["token"] == "secret"
        assert seen["body"]["endpoint"] == "https://cell-1.example"

    async def test_an_unreachable_registry_does_not_raise(
        self, registration, monkeypatch
    ):
        """Fail OPEN: the cell must still start and serve its existing tenants."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("global tier is down", request=request)

        self._mock_transport(monkeypatch, handler)

        assert await register_cell("http://identity:8003", registration, token="s") is False

    async def test_a_rejected_registration_is_reported_not_raised(
        self, registration, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "endpoint is required"})

        self._mock_transport(monkeypatch, handler)

        assert await register_cell("http://identity:8003", registration, token="s") is False

    async def test_it_does_nothing_without_a_token(self, registration, monkeypatch):
        """No token means the internal API would reject it anyway; skipping the
        call keeps a misconfigured dev install from logging a failure per boot."""
        monkeypatch.delenv("IDENTITY_SERVICE_INTERNAL_TOKEN", raising=False)

        assert await register_cell("http://identity:8003", registration) is False

    async def test_it_does_nothing_without_a_url(self, registration):
        assert await register_cell("", registration, token="secret") is False
