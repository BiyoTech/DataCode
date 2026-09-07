"""MLflow tracking resource (Phase 2 placeholder).

Reserved for experiment tracking / model registry integration. Kept as a thin
stub so the resource wiring exists without pulling MLflow as a hard dependency
yet. Implement the tracking methods when the ML workflow phase begins.
"""

from __future__ import annotations

from dagster import ConfigurableResource


class MLflowResource(ConfigurableResource):
    """Placeholder MLflow tracking resource.

    Attributes:
        tracking_uri: MLflow tracking server URI (empty = disabled).
        experiment: Default experiment name for runs.
    """

    tracking_uri: str = ""
    experiment: str = "default"

    @property
    def enabled(self) -> bool:
        return bool(self.tracking_uri)
