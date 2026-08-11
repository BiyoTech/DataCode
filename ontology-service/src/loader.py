"""YAML loader and model registry for ontology files.

``SemanticModel.from_yaml`` is defined on the model class itself (in
``models.py``).  This module provides:

* ``ModelRegistry`` — auto-discovers ontology YAML files from a SQLMesh
  workspace's ``ontology/`` sub-directory, supports lightweight listing
  and lazy full-load with caching.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from src.models import ModelSummary, SemanticModel

logger = structlog.get_logger(__name__)


class ModelRegistry:
    """Auto-discovers ontologies from ``{workspace}/ontology/*.yaml``.

    The registry scans the ``ontology/`` sub-directory of the given SQLMesh
    workspace path.  Each ``.yaml`` file represents one ontology whose name
    is the file stem (without extension).

    ``list_models`` performs a lightweight parse (top-level ``description``
    and ``ai_context`` only) for efficiency.  ``get_model`` performs a full
    Pydantic-validated parse and caches the result.
    """

    def __init__(self, workspace_path: Path) -> None:
        """Initialise the registry with a SQLMesh workspace path.

        Args:
            workspace_path: Root directory of the SQLMesh workspace.
                The registry looks for ``{workspace_path}/ontology/``.
        """
        self._workspace_path = Path(workspace_path)
        self._ontology_dir = self._workspace_path / "ontology"
        self._cache: dict[str, SemanticModel] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_models(self) -> list[ModelSummary]:
        """List all available ontologies (lightweight, no full parse).

        Returns:
            Alphabetically sorted list of model summaries with name,
            description, and keywords (from ai_context.synonyms).
        """
        summaries: list[ModelSummary] = []

        for yaml_file in sorted(self._ontology_dir.glob("*.yaml")):
            name = yaml_file.stem
            try:
                summary = self._parse_summary(yaml_file, name)
                summaries.append(summary)
            except Exception:
                logger.error(
                    "Failed to parse ontology summary",
                    file=str(yaml_file),
                    ontology_name=name,
                    exc_info=True,
                )

        logger.info(
            "Listed ontology models",
            workspace=str(self._workspace_path),
            count=len(summaries),
        )
        return summaries

    def get_model(self, name: str) -> SemanticModel:
        """Lazy-load and cache a full SemanticModel by ontology name.

        Args:
            name: Ontology name (matches the YAML file stem).

        Returns:
            The fully validated SemanticModel.

        Raises:
            FileNotFoundError: If ``{ontology_dir}/{name}.yaml`` does not exist.
            ValueError: If the YAML content fails Pydantic validation.
        """
        if name in self._cache:
            return self._cache[name]

        yaml_path = self._resolve_ontology_path(name)
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"Ontology '{name}' not found at {yaml_path}"
            )

        logger.info("Loading ontology model", ontology_name=name, path=str(yaml_path))
        model = SemanticModel.from_yaml(yaml_path, name=name)

        entity_count = len(model.entities)
        rel_count = len(model.relationships)
        metric_count = len(model.metrics)
        logger.info(
            "Ontology model loaded",
            ontology_name=name,
            entities=entity_count,
            relationships=rel_count,
            metrics=metric_count,
        )

        self._cache[name] = model
        return model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_ontology_path(self, name: str) -> Path:
        """Resolve an ontology name to a YAML file path with traversal guard.

        Ensures the resolved path stays within the ``ontology/`` directory
        to prevent path-traversal attacks (e.g. ``name="../../../etc/passwd"``).
        """
        ontology_dir = self._ontology_dir.resolve()
        # Construct candidate path and resolve it
        candidate = (self._ontology_dir / f"{name}.yaml").resolve()

        # Security: verify the resolved path is inside ontology_dir
        try:
            candidate.relative_to(ontology_dir)
        except ValueError:
            raise ValueError(
                f"Invalid ontology name '{name}': resolved path escapes "
                f"the ontology directory"
            )
        return candidate

    @staticmethod
    def _parse_summary(yaml_path: Path, name: str) -> ModelSummary:
        """Parse only top-level description and ai_context from a YAML file.

        This avoids full Pydantic validation for listing efficiency.
        """
        raw: Any = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}

        description = raw.get("description")

        # Extract keywords from ai_context.synonyms (if structured form)
        keywords: list[str] = []
        ai_ctx = raw.get("ai_context")
        if isinstance(ai_ctx, dict):
            syns = ai_ctx.get("synonyms", [])
            if isinstance(syns, list):
                keywords = [str(s) for s in syns]

        return ModelSummary(
            name=name,
            description=description,
            keywords=keywords,
        )
