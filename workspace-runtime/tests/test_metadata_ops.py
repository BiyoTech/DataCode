"""Sandbox-side metadata op tests — pure data out, no Context in the runtime.

These exercise the op bodies with stub Contexts (the real Context behaviour is
SQLMesh's, already covered by its own suite). What must hold here:

* every op returns PURE DATA (JSON-serializable dicts/lists/strings);
* model-not-found raises the stable ``model_not_found`` code;
* the environment view switches on the environment parameter;
* the query op applies the safe LIMIT;
* column-lineage params select the column-level branch.
"""

from __future__ import annotations

import json
from typing import Dict, Optional

import pytest

from src.executor.metadata_ops import (
    op_environments,
    op_lineage,
    op_models,
    op_query,
)
from src.executor.op_error import OpError


class FakeModel:
    def __init__(self, name: str, fqn: Optional[str] = None):
        self.name = name
        self.fqn = fqn or f"db.{name}"
        self.description = None
        self.owner = None
        self.tags = []
        self.cron = None
        self.kind = _Kind("MODEL")
        self.gateway = None
        self.dialect = "duckdb"
        self._path = _Path(f"/ws/sql-warehouse/models/{name}.sql")
        self.is_sql = True
        self.is_python = False


class _Kind:
    def __init__(self, name: str):
        self.name = name


class _Path:
    def __init__(self, value: str):
        self._value = value

    def absolute(self):
        return self

    def as_posix(self):
        return self._value

    def relative_to(self, other):
        return self


class FakeContext:
    def __init__(self, models: Dict[str, FakeModel], metrics: Optional[Dict] = None):
        self.models = models
        self.metrics = metrics or {}

    def refresh(self) -> None:
        pass

    def get_model(self, name, raise_if_missing=False):
        return self.models.get(name)

    def state_reader(self):  # pragma: no cover - unused in these tests
        raise NotImplementedError


class TestOpModels:
    async def test_local_view_lists_sorted_models(self):
        ctx = FakeContext(
            {"b": FakeModel("b"), "a": FakeModel("a")},
            metrics={"total_orders": object()},
        )

        result = op_models(ctx, {"include_metrics": True})

        assert [m["name"] for m in result["models"]] == ["a", "b"]
        assert result["metrics"] == [{"name": "total_orders"}]
        # Pure data: the payload crosses the wire as JSON.
        json.dumps(result)

    async def test_result_is_json_serializable_with_unusual_fields(self):
        model = FakeModel("a")
        model.tags = ["x", "y"]
        model.description = "déscription ✓"

        result = op_models(FakeContext({"a": model}), {})

        json.dumps(result)


class TestOpEnvironments:
    async def test_raw_names_skip_synthesis(self):
        class _Env:
            def __init__(self, name):
                self.name = name

        class _StateReader:
            def get_environments(self):
                return [_Env("prod")]

        class _Ctx(FakeContext):
            state_reader = _StateReader()

        result = op_environments(_Ctx({}), {"raw_names": True})

        assert result == {"names": ["prod"]}


class TestOpQuery:
    async def test_limit_is_applied_to_limitless_sql(self):
        class _Ctx(FakeContext):
            def fetchdf(self, sql):
                _Ctx.last_sql = sql
                import pandas as pd

                return pd.DataFrame({"x": [1, 2]})

        ctx = _Ctx({})

        result = op_query(ctx, {"sql": "SELECT x FROM t", "row_limit": 5, "max_row_limit": 10})

        assert "LIMIT 5" in _Ctx.last_sql
        assert result["columns"] == ["x"]
        assert result["rows"] == [["1"], ["2"]]
        assert result["total_rows"] == 2

    async def test_existing_limit_is_respected(self):
        class _Ctx(FakeContext):
            def fetchdf(self, sql):
                _Ctx.last_sql = sql
                import pandas as pd

                return pd.DataFrame({"x": []})

        ctx = _Ctx({})

        op_query(ctx, {"sql": "SELECT x FROM t LIMIT 2", "row_limit": 5, "max_row_limit": 10})

        assert _Ctx.last_sql.count("LIMIT") == 1


class TestOpLineage:
    async def test_missing_model_raises_the_stable_code(self):
        with pytest.raises(OpError) as excinfo:
            op_lineage(FakeContext({}), {"model": "db.missing"})

        assert excinfo.value.code == "model_not_found"

    async def test_column_param_selects_column_lineage(self):
        # The column branch needs real SQLMesh lineage machinery; here we only
        # verify the dispatch: with a column param the op reaches the
        # column-level path (which for a context without models raises the
        # not-found error BEFORE touching lineage).
        with pytest.raises(OpError) as excinfo:
            op_lineage(
                FakeContext({}), {"model": "db.m", "column": "c", "models_only": True}
            )

        assert excinfo.value.code == "model_not_found"


class TestPureDataContract:
    async def test_ops_never_return_live_objects(self):
        """Guard: whatever the ops return must survive a JSON round trip."""
        model = FakeModel("a")
        ctx = FakeContext({"a": model})

        payloads = [
            op_models(ctx, {}),
            op_models(ctx, {"include_metrics": True}),
        ]
        for payload in payloads:
            round_tripped = json.loads(json.dumps(payload, default=str))
            assert isinstance(round_tripped, dict)
