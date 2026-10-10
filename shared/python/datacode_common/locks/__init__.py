"""Cross-service locking primitives.

A lock protocol only works if every participant implements it identically, so
these live here rather than being copied per service.
"""

from datacode_common.locks.env_locks import (
    EnvironmentLockConfig,
    EnvironmentLockConfigError,
    EnvironmentLockRegistry,
    EnvironmentLocks,
    environment_lock_key,
)

__all__ = [
    "EnvironmentLockConfig",
    "EnvironmentLockConfigError",
    "EnvironmentLockRegistry",
    "EnvironmentLocks",
    "environment_lock_key",
]
