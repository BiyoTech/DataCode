"""Ontology subsystem configuration.

The ontology surface runs inside workspace-runtime, so it has no configuration
of its own to resolve: the one value it needs — where working copies live — is
the SAME value the rest of this service already resolved in ``src.config``.

It used to re-resolve that path from the platform file plus its own
``ONTOLOGY_WORKSPACE_BASE_PATH`` env var, a name no deployment ever declared.
Worse than dead: had anyone set it, the ontology routes and the path validator
would have disagreed about the base directory inside one process. Delegating to
the service's single resolution removes the disagreement by construction.

Secrets/URLs are never hard-coded; see ``src.config`` for the resolution order.
"""

from __future__ import annotations

from src.config import settings as service_settings


class Settings:
    """The ontology subsystem's view of this service's settings."""

    def __init__(self) -> None:
        # Same object, re-exposed under the name the ontology modules were
        # written against. One resolution, one place it can change.
        self.workspace_base_path: str = service_settings.workspace_base_path


# Module-level singleton used across the ontology modules.
settings = Settings()
