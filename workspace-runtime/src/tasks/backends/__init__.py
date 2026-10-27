"""Backend selection for the task execution plane."""

from src.tasks.backends.base import TaskBackend, TaskBackendError

__all__ = ["TaskBackend", "TaskBackendError", "build_backend"]


def build_backend(backend_name: str) -> TaskBackend:
    """Instantiate the configured backend.

    Imports lazily so a deployment does not need the Kubernetes client to run
    the local backend, and vice versa.

    Raises:
        TaskBackendError: Unknown name, or the backend cannot initialize.
    """
    from src.isolation.policy import BACKEND_KUBERNETES, BACKEND_LOCAL

    normalized = (backend_name or "").strip().lower()
    if normalized == BACKEND_LOCAL:
        from src.tasks.backends.local import LocalProcessBackend

        return LocalProcessBackend()
    if normalized == BACKEND_KUBERNETES:
        from src.tasks.backends.kubernetes import KubernetesBackend

        return KubernetesBackend()

    raise TaskBackendError(
        f"unknown task backend {backend_name!r}; "
        f"expected {BACKEND_LOCAL!r} or {BACKEND_KUBERNETES!r}"
    )
