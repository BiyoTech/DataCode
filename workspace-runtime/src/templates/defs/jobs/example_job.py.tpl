"""Example asset job for workspace {{ workspace_name }}.

An asset job materializes a selection of assets together. This one targets the
Daft example asset; broaden the selection as your pipeline grows.

Auto-discovered by ``definitions.py``: the top-level job object is picked up
directly.
"""

import dagster as dg

# Materialize the Daft example feature asset. Use ``AssetSelection`` to widen or
# narrow the set (e.g. ``dg.AssetSelection.groups("daft")``).
example_job = dg.define_asset_job(
    name="example_job",
    selection=dg.AssetSelection.assets("example_features"),
)
