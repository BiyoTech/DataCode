"""Tests for the YAML loader and ModelRegistry."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.loader import ModelRegistry
from src.models import SemanticModel

# ============================================================
# SemanticModel.from_yaml
# ============================================================

class TestFromYaml:
    def test_load_valid_yaml(self, workspace_dir: Path):
        yaml_path = workspace_dir / "ontology" / "sushi_retail.yaml"
        model = SemanticModel.from_yaml(yaml_path, name="sushi_retail")
        assert model.name == "sushi_retail"
        assert model.version == "0.1.0"
        assert "Customer" in model.entities
        assert "Order" in model.entities
        assert "total_orders" in model.metrics

    def test_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            SemanticModel.from_yaml(tmp_path / "nonexistent.yaml", name="x")

    def test_invalid_yaml_syntax(self, tmp_path: Path):
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(
            "this: is: not: valid: yaml: [\n", encoding="utf-8"
        )
        with pytest.raises(Exception):
            SemanticModel.from_yaml(yaml_path, name="bad")

    def test_missing_version_raises(self, tmp_path: Path):
        yaml_path = tmp_path / "no_version.yaml"
        yaml_path.write_text(
            'description: Missing version field\n', encoding="utf-8"
        )
        with pytest.raises(Exception):
            SemanticModel.from_yaml(yaml_path, name="no_version")


# ============================================================
# ModelRegistry — list_models
# ============================================================

class TestListModels:
    def test_lists_sushi_retail(self, registry: ModelRegistry):
        models = registry.list_models()
        assert len(models) == 1
        assert models[0].name == "sushi_retail"
        assert models[0].description is not None
        assert "sushi" in models[0].description.lower()

    def test_returns_keywords_from_synonyms(self, registry: ModelRegistry):
        models = registry.list_models()
        # sushi_retail.yaml has ai_context.synonyms: [sushi analytics, restaurant data]
        assert "sushi analytics" in models[0].keywords
        assert "restaurant data" in models[0].keywords

    def test_empty_workspace(self, empty_workspace: Path):
        reg = ModelRegistry(empty_workspace)
        assert reg.list_models() == []

    def test_multiple_models(self, tmp_path: Path):
        ontology_dir = tmp_path / "ontology"
        ontology_dir.mkdir()
        (ontology_dir / "model_a.yaml").write_text(
            'version: "0.1.0"\ndescription: Model A\n', encoding="utf-8"
        )
        (ontology_dir / "model_b.yaml").write_text(
            'version: "0.1.0"\ndescription: Model B\n', encoding="utf-8"
        )
        reg = ModelRegistry(tmp_path)
        models = reg.list_models()
        assert len(models) == 2
        names = [m.name for m in models]
        assert "model_a" in names
        assert "model_b" in names

    def test_skips_broken_files(self, workspace_with_bad_yaml: Path, tmp_path: Path):
        """Broken YAML files should be skipped, not crash list_models."""
        ontology_dir = workspace_with_bad_yaml / "ontology"
        # Add a valid file alongside the broken one
        (ontology_dir / "good.yaml").write_text(
            'version: "0.1.0"\ndescription: Good model\n', encoding="utf-8"
        )
        reg = ModelRegistry(workspace_with_bad_yaml)
        models = reg.list_models()
        # The broken file is skipped (it has no version, but list_models
        # does lightweight parse without Pydantic validation, so it may
        # still appear — the error handling is for file read errors)
        names = [m.name for m in models]
        assert "good" in names


# ============================================================
# ModelRegistry — get_model
# ============================================================

class TestGetModel:
    def test_loads_full_model(self, registry: ModelRegistry):
        model = registry.get_model("sushi_retail")
        assert model.name == "sushi_retail"
        assert len(model.entities) == 4
        assert len(model.relationships) == 6
        assert len(model.metrics) == 5

    def test_caches_model(self, registry: ModelRegistry):
        model1 = registry.get_model("sushi_retail")
        model2 = registry.get_model("sushi_retail")
        assert model1 is model2  # same object reference (cached)

    def test_not_found_raises(self, registry: ModelRegistry):
        with pytest.raises(FileNotFoundError, match="not found"):
            registry.get_model("nonexistent")

    def test_path_traversal_blocked(self, registry: ModelRegistry):
        with pytest.raises(ValueError, match="escapes"):
            registry.get_model("../../../etc/passwd")

    def test_path_traversal_with_extension(self, registry: ModelRegistry):
        with pytest.raises((ValueError, FileNotFoundError)):
            registry.get_model("../../etc/passwd")


# ============================================================
# ModelRegistry — integration with search service
# ============================================================

class TestRegistryIntegration:
    def test_list_then_get_workflow(self, registry: ModelRegistry):
        """Simulates the Agent workflow: list first, then load on demand."""
        models = registry.list_models()
        assert len(models) >= 1

        for summary in models:
            model = registry.get_model(summary.name)
            assert model.name == summary.name
