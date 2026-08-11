"""Shared test fixtures for ontology-service tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import pytest

from src.loader import ModelRegistry
from src.models import SemanticModel
from src.semantic_search import SemanticSearchService

# Path to the example ontology YAML shipped with the project
EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
SUSHI_YAML = EXAMPLES_DIR / "sushi_retail.yaml"


# ------------------------------------------------------------------
# Workspace fixtures
# ------------------------------------------------------------------

@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Create a temporary SQLMesh workspace with an ontology/ directory
    containing a copy of sushi_retail.yaml."""
    ontology_dir = tmp_path / "ontology"
    ontology_dir.mkdir()
    shutil.copy2(SUSHI_YAML, ontology_dir / "sushi_retail.yaml")
    return tmp_path


@pytest.fixture
def empty_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with an empty ontology/ directory."""
    ontology_dir = tmp_path / "ontology"
    ontology_dir.mkdir()
    return tmp_path


@pytest.fixture
def workspace_with_bad_yaml(tmp_path: Path) -> Path:
    """Create a workspace with an invalid ontology YAML for error testing."""
    ontology_dir = tmp_path / "ontology"
    ontology_dir.mkdir()
    # YAML with missing required fields (no version)
    (ontology_dir / "broken.yaml").write_text(
        "description: A broken ontology\n", encoding="utf-8"
    )
    return tmp_path


# ------------------------------------------------------------------
# Model and service fixtures
# ------------------------------------------------------------------

@pytest.fixture
def sushi_model(workspace_dir: Path) -> SemanticModel:
    """Load the sushi_retail ontology as a SemanticModel."""
    return SemanticModel.from_yaml(
        workspace_dir / "ontology" / "sushi_retail.yaml",
        name="sushi_retail",
    )


@pytest.fixture
def registry(workspace_dir: Path) -> ModelRegistry:
    """Create a ModelRegistry pointing at the test workspace."""
    return ModelRegistry(workspace_dir)


@pytest.fixture
def search_service(registry: ModelRegistry) -> SemanticSearchService:
    """Create a SemanticSearchService with the test registry."""
    return SemanticSearchService(registry)


# ------------------------------------------------------------------
# YAML construction helpers
# ------------------------------------------------------------------

@pytest.fixture
def make_ontology_yaml(tmp_path: Path):
    """Factory fixture to create custom ontology YAML files in a workspace."""

    def _make(name: str, content: str) -> Path:
        ontology_dir = tmp_path / "ontology"
        ontology_dir.mkdir(exist_ok=True)
        yaml_path = ontology_dir / f"{name}.yaml"
        yaml_path.write_text(dedent(content), encoding="utf-8")
        return tmp_path  # return workspace root

    return _make
