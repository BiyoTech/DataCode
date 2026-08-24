"""Tests for the Dagster deployment binder (environments.deployment_binder).

The binder is the registry's only view of dagster-service, so its honest-read
contract is pinned here:

* ``list_deployments`` returns ``None`` on transport errors, malformed bodies
  and non-2xx answers — unreachable is NOT an empty list;
* ``ensure_deployment`` / ``destroy_deployment`` report False instead of
  raising, so orchestrations record drift rather than fail;
* the destroy path targets the URL-encoded deployment name.

All traffic goes through ``httpx.MockTransport``: no real network.
"""

from __future__ import annotations

import json
import typing as t

import httpx
import pytest

from src.environments.deployment_binder import DeploymentBinder

_BASE = "http://dagster.test"


def _binder(handler) -> DeploymentBinder:
    return DeploymentBinder(
        base_url=_BASE,
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


async def test_list_returns_the_served_deployments():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/dagster/deployments"
        return httpx.Response(
            200, json=[{"deployment_name": "ws__prod", "workspace_id": "ws"}]
        )

    served = await _binder(handler).list_deployments()

    assert served == [{"deployment_name": "ws__prod", "workspace_id": "ws"}]


async def test_list_returns_none_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert await _binder(handler).list_deployments() is None


async def test_list_returns_none_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    assert await _binder(handler).list_deployments() is None


async def test_list_returns_none_on_malformed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "dict"})

    assert await _binder(handler).list_deployments() is None


async def test_ensure_posts_the_policy_and_reports_success():
    seen: dict[str, t.Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"deployment_name": "ws__dev_alice"})

    ok = await _binder(handler).ensure_deployment(
        workspace_path="/base/wt/dev_alice",
        environment="dev_alice",
        env_type="sandbox",
        schedules_enabled=False,
    )

    assert ok is True
    assert seen["path"] == "/api/v1/dagster/deployments/ensure"
    assert seen["body"] == {
        "workspace_path": "/base/wt/dev_alice",
        "environment": "dev_alice",
        "env_type": "sandbox",
        "schedules_enabled": False,
    }


async def test_ensure_reports_false_on_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    ok = await _binder(handler).ensure_deployment(
        workspace_path="/base/wt/dev_alice",
        environment="dev_alice",
        env_type="sandbox",
        schedules_enabled=False,
    )

    assert ok is False


async def test_destroy_deletes_the_named_deployment():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        # URL-encoded name in the path, no query noise.
        assert request.url.path == "/api/v1/dagster/deployments/ws__dev_alice"
        return httpx.Response(200, json={"success": True})

    ok = await _binder(handler).destroy_deployment(deployment_name="ws__dev_alice")

    assert ok is True


async def test_destroy_reports_false_on_refusal_or_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert (
        await _binder(handler).destroy_deployment(deployment_name="ws__dev_alice")
        is False
    )


async def test_destroy_of_an_empty_name_touches_nothing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be sent for an empty name")

    assert (
        await _binder(handler).destroy_deployment(deployment_name="") is False
    )


@pytest.mark.parametrize("base", ["http://dagster.test/", "http://dagster.test"])
def test_base_url_trailing_slash_is_normalized(base):
    binder = DeploymentBinder(
        base_url=base,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )
    assert binder._base_url == "http://dagster.test"  # noqa: SLF001
