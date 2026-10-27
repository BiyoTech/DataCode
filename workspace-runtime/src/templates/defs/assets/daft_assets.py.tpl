"""Daft feature-engineering assets for workspace {{ workspace_name }}.

Example Data+AI asset showing how to compute features with Daft. Replace the
body with your own transforms; the pattern (return a materialized result and
emit metadata) stays the same.

Auto-discovered by ``definitions.py``: the top-level ``@asset`` is picked up
directly, no manual registration needed.
"""

from dagster import AssetExecutionContext, MaterializeResult, asset


@asset(group_name="daft", kinds={"daft"})
def example_features(context: AssetExecutionContext) -> MaterializeResult:
    """Compute example features with Daft.

    This is a scaffold: it builds a tiny in-memory DataFrame so the asset runs
    out of the box. Swap ``from_pydict`` for ``read_parquet`` / ``read_csv``
    against your warehouse outputs.
    """
    import daft

    df = daft.from_pydict({"id": [1, 2, 3], "value": [10.0, 20.0, 30.0]})
    # The total is aggregated FIRST and then applied as a scalar: Daft allows an
    # aggregation only in agg/pivot/window or a global select, so
    # ``df["value"] / df["value"].sum()`` raises DaftCoreException. Pinned by
    # tests/test_scaffold_dependencies.py, which executes this body.
    total = df.agg(daft.col("value").sum()).to_pydict()["value"][0]
    df = df.with_column("value_scaled", df["value"] / total)
    df = df.collect()

    row_count = df.count_rows()
    context.log.info(f"example_features computed {row_count} rows")
    return MaterializeResult(metadata={"row_count": row_count})
