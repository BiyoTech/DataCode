"""The registry write channel: runtime authors the fact, control stores it.

``environments`` / ``environment_events`` belong to control-service, but two
facts about them originate here — the watermark a successful apply leaves, and
the audit row a merge produces. They travel over
``/internal/env-registry/*`` instead of being written locally.

What must hold:

* the request carries exactly the fact, and the control plane applies its own
  rule (no generic "update this row" escape hatch);
* every failure mode — unreachable, rejected, unconfigured — is swallowed:
  both calls describe an operation that ALREADY happened, so failing it would
  hide a completed action;
* an unconfigured channel says so, loudly enough to find in a log, and does
  not pretend the write succeeded.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.environments.registry_client import EnvironmentRegistryClient


def _client(
    handler, *, base_url: str = "http://control:8004", token: str = "t"
) -> EnvironmentRegistryClient:
    return EnvironmentRegistryClient(
        base_url=base_url,
        token=token,
        transport=httpx.MockTransport(handler),
    )


class TestAppliedWatermark:
    async def test_the_fact_is_sent_and_confirmed(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["token"] = request.headers.get("X-Internal-Token")
            seen["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={"updated": True})

        ok = await _client(handler).mark_applied(
            "ws-t1-shop:prod", commit_hash="abc123", dirty=True
        )

        assert ok is True
        assert seen["path"] == "/internal/env-registry/applied"
        assert seen["token"] == "t"
        assert seen["body"]["environment_id"] == "ws-t1-shop:prod"
        assert seen["body"]["commit_hash"] == "abc123"
        assert seen["body"]["dirty"] is True

    async def test_an_unknown_environment_is_reported_not_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"updated": False})

        assert await _client(handler).mark_applied("gone", commit_hash="x") is False

    async def test_a_control_plane_outage_does_not_fail_the_apply(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("control is down", request=request)

        assert await _client(handler).mark_applied("e", commit_hash="x") is False

    async def test_a_rejected_token_does_not_fail_the_apply(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "internal token required"})

        assert await _client(handler).mark_applied("e", commit_hash="x") is False


class TestEvents:
    async def test_the_event_is_sent_whole(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={"event_id": "ev-1"})

        ok = await _client(handler).record_event(
            environment_id="ws-t1-shop:prod",
            action="merge",
            actor_user_id="u1",
            succeeded=False,
            commit_hash="deadbeef",
            detail={"from": "dev"},
        )

        assert ok is True
        assert seen["path"] == "/internal/env-registry/events"
        assert seen["body"]["action"] == "merge"
        assert seen["body"]["actor_user_id"] == "u1"
        assert seen["body"]["succeeded"] is False
        assert seen["body"]["detail"] == {"from": "dev"}

    async def test_an_outage_does_not_fail_the_merge(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        assert await _client(handler).record_event(
            environment_id="e", action="merge"
        ) is False


class TestUnconfigured:
    """No address or no token: report it, never silently claim success."""

    @pytest.mark.parametrize(
        "base_url,token", [("", "t"), ("http://control:8004", ""), ("", "")]
    )
    async def test_nothing_is_dialled(self, base_url, token):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not dial without a configured channel")

        client = _client(handler, base_url=base_url, token=token)

        assert await client.mark_applied("e", commit_hash="x") is False
        assert await client.record_event(environment_id="e", action="merge") is False


class TestAgainstTheRealEndpoint:
    """The client's payload must satisfy control-service's REAL request models.

    Shapes drift when each side is tested against its own fixture, so the
    body the client actually sends is validated by the schema the control
    plane actually declares — imported from its source, not restated here.
    """

    async def test_the_payloads_match_the_control_plane_schema(self):
        import sys
        from pathlib import Path

        control_root = Path(__file__).resolve().parents[2] / "control-service"
        module_path = control_root / "src" / "api" / "rest" / "env_registry.py"
        if not module_path.exists():
            pytest.skip("control-service source not available")

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[request.url.path] = json.loads(request.read().decode())
            return httpx.Response(200, json={"updated": True, "event_id": "x"})

        client = _client(handler)
        await client.mark_applied("ws:prod", commit_hash="abc", dirty=True)
        await client.record_event(environment_id="ws:prod", action="merge")

        # Load the module by path: importing `src.api.rest.env_registry` would
        # collide with this service's own `src` package.
        import importlib.util

        spec = importlib.util.spec_from_file_location("_control_env_registry", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(control_root))
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - control deps may be absent
            pytest.skip(f"control-service module not importable here: {exc}")
        finally:
            sys.path.remove(str(control_root))

        module.AppliedMarkRequest(**captured["/internal/env-registry/applied"])
        module.EventRequest(**captured["/internal/env-registry/events"])
