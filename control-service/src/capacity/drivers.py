"""Replica driver construction from settings.

Kept out of ``lease_controller`` so the controller module holds the POLICY and
the drivers hold the MECHANICS; this file is the only place that knows which
settings knob selects which mechanism.
"""

from __future__ import annotations

from src.capacity.lease_controller import (
    KubernetesReplicaDriver,
    LoggingReplicaDriver,
    ReplicaDriver,
)
from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


def build_replica_driver() -> ReplicaDriver:
    """Select the driver configured for this deployment.

    The logging driver is the default because control-service is deliberately
    light: a dev environment (or a deployment without cluster credentials)
    gets the same DECISION LOGS as production without the kubernetes SDK.
    """
    kind = settings.capacity_driver.strip().lower()
    if kind == "kubernetes":
        driver: ReplicaDriver = KubernetesReplicaDriver(
            namespace=settings.capacity_k8s_namespace,
            statefulset=settings.capacity_k8s_statefulset,
        )
        logger.info(
            "replica_driver_selected",
            driver="kubernetes",
            namespace=settings.capacity_k8s_namespace,
            statefulset=settings.capacity_k8s_statefulset,
        )
        return driver

    if kind != "log":
        logger.warning(
            "replica_driver_unknown_falling_back_to_log",
            configured=kind,
        )
    return LoggingReplicaDriver()
