"""Platform Environment registry.

An ``Environment`` is the platform's single first-class deployment unit: it
binds ONE Git branch, ONE SQLMesh virtual environment and ONE Dagster
deployment together. This package owns the registry (source of truth) and the
binders that reconcile the downstream systems FROM it.

Modules are intentionally layered so the pure parts stay unit-testable:

- :mod:`naming`         pure name algebra (env <-> branch <-> deployment)
- :mod:`models`         ORM models (registry + audit trail)
- :mod:`repository`     database CRUD
- :mod:`git_binder`     Git branch / worktree lifecycle
- :mod:`sqlmesh_binder` SQLMesh virtual environment lifecycle
- :mod:`guard`          runtime admission control for SQLMesh operations
- :mod:`registry`       orchestration + state machine
"""
