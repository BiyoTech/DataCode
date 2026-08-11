"""Tests for the SQLMesh -> Dagster asset bridge (mapping + spec building)."""

import pytest

from src.bridge.sqlmesh_bridge import (
    SQLMeshBridge,
    asset_key_to_str,
    model_name_to_asset_key,
)
from src.config import Settings


def test_model_name_to_asset_key_strips_quotes_and_prefixes():
    assert model_name_to_asset_key('"marts"."orders"') == [
        "sqlmesh",
        "marts",
        "orders",
    ]


def test_model_name_to_asset_key_plain():
    assert model_name_to_asset_key("marts.orders") == ["sqlmesh", "marts", "orders"]


def test_asset_key_to_str_joins_with_double_underscore():
    assert asset_key_to_str(["sqlmesh", "marts", "orders"]) == "sqlmesh__marts__orders"


async def test_build_source_asset_specs_maps_graph_by_display_name(monkeypatch):
    bridge = SQLMeshBridge(Settings())

    async def fake_lineage(_workspace_path, gateway=""):
        # Lineage returns FQN-keyed graph plus a nodes map that carries the
        # display name; asset keys must be derived from the display name.
        return {
            "graph": {
                '"db"."marts"."orders"': ['"db"."staging"."raw_orders"'],
                '"db"."staging"."raw_orders"': [],
            },
            "nodes": {
                '"db"."marts"."orders"': {"name": "marts.orders"},
                '"db"."staging"."raw_orders"': {"name": "staging.raw_orders"},
            },
        }

    monkeypatch.setattr(bridge, "fetch_lineage", fake_lineage)

    specs = await bridge.build_source_asset_specs("/ws/demo")
    by_key = {s["asset_key_str"]: s for s in specs}

    assert "sqlmesh__marts__orders" in by_key
    assert by_key["sqlmesh__marts__orders"]["kind"] == "sqlmesh"
    assert by_key["sqlmesh__marts__orders"]["name"] == "marts.orders"
    assert by_key["sqlmesh__marts__orders"]["deps"] == ["sqlmesh__staging__raw_orders"]
    assert by_key["sqlmesh__staging__raw_orders"]["deps"] == []


async def test_build_source_asset_specs_falls_back_to_fqn_when_name_missing(monkeypatch):
    bridge = SQLMeshBridge(Settings())

    async def fake_lineage(_workspace_path, gateway=""):
        # No nodes metadata -> fail soft to the FQN.
        return {"graph": {"marts.orders": []}, "nodes": {}}

    monkeypatch.setattr(bridge, "fetch_lineage", fake_lineage)

    specs = await bridge.build_source_asset_specs("/ws/demo")
    assert specs[0]["asset_key_str"] == "sqlmesh__marts__orders"


async def test_build_source_asset_specs_handles_non_dict_lineage(monkeypatch):
    bridge = SQLMeshBridge(Settings())

    async def fake_lineage(_workspace_path, gateway=""):
        return []  # unexpected shape -> treated as empty

    monkeypatch.setattr(bridge, "fetch_lineage", fake_lineage)
    assert await bridge.build_source_asset_specs("/ws/demo") == []
