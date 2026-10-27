"""Warm-sandbox container backend — the executor's L3-base vehicle (P6c).

``executor.mode: container`` turns the warm sandbox from a hardened child
process into a Kubernetes Pod. The PROTOCOL does not change (P0 freeze); only
the transport does: the Pod serves the same length-prefixed frames over TCP
that the child served over the socketpair.

One Pod per (working copy, gateway), mirroring the process-mode child keying:
a Context binds its connection at load time, so two gateways means two
sandboxes, never one sandbox serving the wrong warehouse.

Lifecycle follows the lease-holder's judgement, not Kubernetes': the runtime
creates the Pod when a working copy is first touched and deletes it on
invalidation (git switch, post-apply) or shutdown. There is no readiness probe
— the manager connects after the Pod reports Running and reads the hello frame,
which is the readiness that matters (the frame cannot arrive before the server
is actually serving).

Hardening comes from the ``L3_base`` isolation profile, the same declaration
the task plane reads: profile-driven security context, resource limits,
automount-token off, service links off. A second, divergent hardening list for
the warm sandbox would be exactly the drift the profile loader exists to
prevent.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from datacode_common.workspace_layout import tool_home

from src.config import settings
from src.tasks.backends.kubernetes import (
    WORKSPACE_MOUNT_PATH,
    KubernetesBackend,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Port the sandbox listens on inside the Pod. Single, fixed: nothing else runs
# in that network namespace.
SANDBOX_PORT = 8010

_VALID_NAME = re.compile(r"[^a-z0-9-]")


def _pod_name(workspace_path: str, gateway: str) -> str:
    """A DNS-safe, collision-resistant Pod name for one (copy, gateway)."""
    digest = hex(abs(hash((workspace_path, gateway))))[2:10]
    slug = _VALID_NAME.sub("-", Path(workspace_path).name.lower())[:32].strip("-")
    return f"warm-{slug or 'ws'}-{digest}"


class WarmSandboxBackend:
    """Creates and deletes the Pods behind ``executor.mode: container``."""

    def __init__(self) -> None:
        # Reuses the task backend's cluster client AND its mount/scheduling
        # logic: the working-copy hand-off constraints (subPath narrowing, same
        # node as the lease holder) are identical for a warm sandbox and a
        # one-shot task, and a second copy of that logic would be a second
        # place for T1 to regress.
        self._tasks = KubernetesBackend()
        self._core = self._tasks._core
        self._api = self._tasks._api

    async def start(
        self, workspace_path: str, environment: str, gateway: str = ""
    ) -> tuple[str, str, int]:
        """Create the sandbox Pod and return ``(pod_name, host, port)``.

        Raises:
            TaskBackendError: the cluster is unreachable or the Pod could not
                be created — the same failure class the task plane raises, so
                the manager's restart budget treats both alike.
        """
        name = _pod_name(workspace_path, gateway)
        try:
            await asyncio.to_thread(
                self._create_pod, name, workspace_path, environment
            )
            host = await asyncio.wait_for(
                self._await_address(name), timeout=_POD_READY_TIMEOUT
            )
        except Exception:
            await self.stop(name)
            raise
        logger.info(
            "warm_sandbox_started",
            workspace_path=workspace_path,
            gateway=gateway or None,
            pod=name,
            host=host,
        )
        return name, host, SANDBOX_PORT

    async def stop(self, pod_name: str) -> None:
        """Delete the Pod. Best-effort: an orphaned Pod also dies on node
        recycle, and the next start reuses the same deterministic name only
        after the old object is gone — delete + recreate is the honest path."""
        try:
            await asyncio.to_thread(
                self._core.delete_namespaced_pod,
                pod_name,
                settings.task_namespace,
            )
            logger.info("warm_sandbox_stopped", pod=pod_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "warm_sandbox_stop_failed",
                pod=pod_name,
                error=str(exc),
                hint="the Pod outlives its sandbox; delete it manually if it persists",
            )

    # ── internals ──────────────────────────────────────────────────────

    def _create_pod(
        self, name: str, workspace_path: str, environment: str
    ) -> None:
        """Build and create the sandbox Pod (blocking; call in a thread)."""
        from datacode_common.isolation import IsolationLevel

        from src.isolation.policy import runtime_isolation_policy

        profile = runtime_isolation_policy().profile_for("agent.shell")
        if profile.level is not IsolationLevel.CONTAINER:
            # The policy says this deployment cannot host a container sandbox.
            # Raised rather than downgraded: falling back to the process child
            # would make "container mode" silently mean "process mode".
            from src.executor.process import ExecutorError

            raise ExecutorError(
                "crashed",
                "executor_mode=container but no container profile is available; "
                "set executor_mode=process or enable the container plane",
            )

        volumes, mounts = self._tasks._volumes_for_path(workspace_path, name)
        scheduling = self._tasks._scheduling(profile)

        container = self._api.V1Container(
            name="sandbox",
            image=settings.executor_container_image,
            image_pull_policy=settings.task_runner_image_pull_policy,
            command=["python", "-m", "src.executor.server", "--tcp", str(SANDBOX_PORT)],
            env=[
                # The container's view of the working copy: the MOUNT POINT,
                # not the runtime-side path (which does not exist in here).
                self._api.V1EnvVar(
                    name="DATACODE_WORKSPACE_PATH",
                    value=WORKSPACE_MOUNT_PATH,
                ),
                # A writable HOME inside the mounted copy, under the platform's
                # state directory: the image's own HOME is not writable on a
                # read-only root filesystem, and the copy's ROOT is the user's
                # project, not a place for tool dotfiles.
                self._api.V1EnvVar(
                    name="HOME", value=tool_home(WORKSPACE_MOUNT_PATH)
                ),
                self._api.V1EnvVar(name="TZ", value="UTC"),
            ],
            volume_mounts=mounts,
            resources=self._tasks._resources(profile),
            security_context=self._tasks._container_security_context(profile),
            ports=[
                self._api.V1ContainerPort(name="sandbox", container_port=SANDBOX_PORT)
            ],
        )

        pod_spec_kwargs: dict = {}
        dns_policy = str(profile.settings.get("dns_policy") or "")
        if dns_policy:
            pod_spec_kwargs["dns_policy"] = dns_policy
            dns_config = self._tasks._dns_config()
            if dns_policy == "None" and dns_config is not None:
                pod_spec_kwargs["dns_config"] = dns_config
            elif dns_policy == "None":
                pod_spec_kwargs["dns_policy"] = "ClusterFirst"

        pod = self._api.V1Pod(
            metadata=self._api.V1ObjectMeta(
                name=name,
                namespace=settings.task_namespace,
                labels={
                    "app": "task-runner",  # same policy envelope as one-shot tasks
                    "datacode.io/kind": "warm-sandbox",
                    "datacode.io/workspace": Path(workspace_path).name,
                },
            ),
            spec=self._api.V1PodSpec(
                restart_policy="Never",
                service_account_name=settings.task_service_account,
                automount_service_account_token=False,
                enable_service_links=False,
                host_network=False,
                host_pid=False,
                host_ipc=False,
                node_selector=scheduling.get("node_selector") or None,
                tolerations=scheduling.get("tolerations"),
                containers=[container],
                volumes=volumes,
                **pod_spec_kwargs,
            ),
        )
        self._core.create_namespaced_pod(settings.task_namespace, pod)

    async def _await_address(self, pod_name: str) -> str:
        """Poll the Pod until it has a routable address."""
        while True:
            pod = await asyncio.to_thread(
                self._core.read_namespaced_pod, pod_name, settings.task_namespace
            )
            phase = pod.status.phase if pod.status else ""
            if phase == "Failed":
                raise RuntimeError(f"warm sandbox Pod {pod_name} failed to start")
            pod_ip = (pod.status.pod_ip if pod.status else None) or ""
            if pod_ip:
                return pod_ip
            await asyncio.sleep(0.5)


# How long the Pod may take from creation to a routable address. Image pulls
# dominate; a sandbox that cannot become ready in this window is a cluster
# problem, surfaced as a start failure rather than an infinite wait.
_POD_READY_TIMEOUT = float(settings.executor_container_ready_timeout or 120)

__all__ = ["SANDBOX_PORT", "WarmSandboxBackend"]
