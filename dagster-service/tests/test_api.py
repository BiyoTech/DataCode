"""API integration tests for the health + mode endpoints.

A minimal app including only the health router is used so importing the full
application (and its dagster/daft dependencies + subprocess-spawning lifespan)
is not required to verify request/response contracts.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.rest import health
from src.config import settings


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


def test_health_reports_service_and_mode(client):
    resp = client.get("/api/v1/dagster/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["service"] == "dagster-service"
    assert body["dagster_mode"] == settings.dagster_mode
    assert isinstance(body["processes"], list)


def test_mode_info_reflects_local_settings(client, monkeypatch):
    monkeypatch.setattr(settings, "dagster_mode", "local")
    resp = client.get("/api/v1/dagster/mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "local"
    assert body["code_location_port"] == settings.code_location_port


def test_mode_info_reflects_remote_settings(client, monkeypatch):
    monkeypatch.setattr(settings, "dagster_mode", "remote")
    monkeypatch.setattr(settings, "dagster_remote_graphql_url", "http://remote/graphql")
    resp = client.get("/api/v1/dagster/mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "remote"
    # Remote mode surfaces the remote GraphQL endpoint.
    assert body["graphql_url"] == "http://remote/graphql"
