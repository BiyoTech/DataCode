"""Smoke tests for workspace {{ workspace_name }}'s definitions.

Run with ``pytest`` from the ``asset-dag`` directory. These validate that the
code location loads and that the example asset materializes.
"""

import dagster as dg

from defs.assets.daft_assets import example_features


def test_example_features_materializes() -> None:
    """The Daft example asset should materialize and report a row count."""
    result = dg.materialize([example_features])
    assert result.success

    materialization = result.asset_materializations_for_node("example_features")
    assert materialization, "expected an AssetMaterialization for example_features"
