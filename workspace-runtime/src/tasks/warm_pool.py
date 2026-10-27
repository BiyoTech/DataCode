"""Warm pool — resident task runners, keyed by working copy (design 06 P6c).

Why the pool exists
-------------------
The one-shot vehicle pays a container start per task. For a SQLMesh plan that
cost is dwarfed by the Context load; for ``agent.shell`` it IS the cost — a user
waits 10-30 s to run ``git status``. A resident runner amortizes both.

Keyed by WORKING COPY, not by tenant
------------------------------------
The roadmap sketched "1-2 warm runners per tenant". That cannot be built without
giving up the T1 closure: a Pod serving several of a tenant's working copies must
mount a subtree containing all of them, which makes one member's copy readable
from another member's task — precisely the exposure ``subPath`` was introduced to
remove. So the pool is keyed by working copy and keeps the single-copy mount.

The trade is a lower hit rate: the first task against a copy still starts a Pod.
That is the honest cost of the isolation, and it is bounded — a user works in one
or two copies at a time, so their SECOND task onwards is warm, which is where
the latency complaint actually comes from.

Lifecycle
---------
* **Admission** — a Pod is created on the first poolable task for a copy, up to
  ``warm_pool_max_pods``. Above that the caller falls back to a one-shot Job
  rather than waiting: a queue here would turn a capacity problem into unbounded
  latency.
* **Retirement** — a Pod is retired after ``warm_pool_max_tasks`` tasks or
  ``warm_pool_idle_seconds`` of inactivity. The task cap matters because a task
  can damage its own interpreter (a leaked thread, a segfaulting extension) and
  the next task would inherit it; the idle cap returns capacity.
* **Invalidation** — a git switch or an apply retires the copy's Pod, exactly as
  it retires the warm sandbox: the Pod holds no Context, but it does hold a
  working directory whose contents just changed underneath it.

One task at a time per Pod. Parallelism comes from holding several Pods, not from
concurrency inside one — a second task in a Pod shares its memory limit, CPU
quota and SQLMesh cache directory with the first.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Optional

from datacode_common.framing import ProtocolError, encode, read_frame
from datacode_common.isolation import IsolationLevel, IsolationProfile
from datacode_common.task_protocol import (
    ERR_SUBMIT_FAILED,
    TaskResult,
    TaskSpec,
)
from datacode_common.warm_protocol import (
    ERR_BUSY,
    OP_HELLO,
    OP_PING,
    make_run_request,
    result_from_response,
)
from datacode_common.workspace_layout import tool_home

from src.config import settings
from src.tasks.backends.base import TaskBackendError
from src.tasks.credentials import SandboxCredentials
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Port the resident runner listens on inside its Pod. Fixed: one process per
# network namespace, so there is nothing to allocate.
RUNNER_PORT = 8020

_VALID_NAME = re.compile(r"[^a-z0-9-]")

# How long to wait for the hello frame after connecting. The Pod is already
# Running at that point, so this only covers interpreter start-up.
_HELLO_TIMEOUT_SECONDS = 30.0


def pool_pod_name(workspace_path: str) -> str:
    """Deterministic, DNS-safe Pod name for one working copy's resident runner.

    Deterministic so a runtime restart converges on the same object instead of
    stacking a second Pod beside a half-dead one.
    """
    digest = hex(abs(hash(workspace_path)))[2:10]
    slug = _VALID_NAME.sub("-", Path(workspace_path).name.lower())[:24].strip("-")
    return f"warm-task-{slug or 'ws'}-{digest}"


class _PooledRunner:
    """One resident Pod and the connection to it."""

    def __init__(self, pod_name: str, workspace_path: str, host: str) -> None:
        self.pod_name = pod_name
        self.workspace_path = workspace_path
        self.host = host
        self.tasks_served = 0
        self.last_used = time.monotonic()
        self.kinds: tuple[str, ...] = ()
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    async def connect(self) -> None:
        """Dial the Pod and consume its hello frame.

        The hello is the readiness signal that matters: it cannot arrive before
        the runner is actually serving, which a TCP-accept probe or a readiness
        probe on the port cannot promise.
        """
        self._reader, self._writer = await asyncio.open_connection(
            self.host, RUNNER_PORT
        )
        hello = await asyncio.wait_for(
            read_frame(self._reader), timeout=_HELLO_TIMEOUT_SECONDS
        )
        if hello.get("ev") != OP_HELLO:
            raise TaskBackendError(
                f"resident runner {self.pod_name} did not greet with hello: {hello!r}"
            )
        data = hello.get("data") or {}
        served = str(data.get("workspace_path") or "")
        if served != self.workspace_path:
            # The Pod is mounted on a different copy than we intend to send it.
            # Refused rather than used: this is a routing bug, and executing the
            # task anyway would run it against the wrong project.
            raise TaskBackendError(
                f"resident runner {self.pod_name} serves {served!r}, "
                f"expected {self.workspace_path!r}"
            )
        self.kinds = tuple(str(k) for k in (data.get("kinds") or ()))

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass

    def implements(self, kind: str) -> bool:
        """Whether the Pod's image implements a kind.

        Checked before sending: a stale image is otherwise discovered as a failed
        task, which reads to the user as "my command is broken".
        """
        return not self.kinds or kind in self.kinds

    async def run(
        self, spec: TaskSpec, credentials: Optional[SandboxCredentials]
    ) -> TaskResult:
        """Send one task and await its result. One in flight per Pod."""
        async with self._lock:
            if self._writer is None or self._reader is None:
                raise TaskBackendError(f"runner {self.pod_name} is not connected")
            self._next_id += 1
            request = make_run_request(
                self._next_id,
                spec,
                # Per task, never per Pod: the resident process applies these for
                # the duration of this task and drops them afterwards.
                credentials.env if credentials is not None else None,
            )
            self._writer.write(encode(request))
            await self._writer.drain()

            # The runner arms its own deadline, so this bound only needs to
            # exceed it; without any bound a wedged Pod would hold the caller
            # forever.
            response = await asyncio.wait_for(
                read_frame(self._reader), timeout=spec.timeout_seconds + 30
            )
        self.tasks_served += 1
        self.last_used = time.monotonic()
        if not response:
            raise TaskBackendError(
                f"runner {self.pod_name} closed the connection without a result"
            )
        return response


class WarmPool:
    """Holds resident runner Pods, one per working copy."""

    def __init__(self) -> None:
        self._runners: dict[str, _PooledRunner] = {}
        self._pods: Optional[Any] = None
        self._lock = asyncio.Lock()

    # ── admission ─────────────────────────────────────────────────────

    def _backend(self) -> Any:
        """The Kubernetes task backend, for its client and its Pod helpers.

        Lazy: the kubernetes client is an optional dependency, and the pool is
        off by default, so importing it eagerly would break every dev install.
        """
        if self._pods is None:
            from src.tasks.backends.kubernetes import KubernetesBackend

            self._pods = KubernetesBackend()
        return self._pods

    def poolable(self, kind: str, profile: IsolationProfile) -> bool:
        """Whether a task may run on a resident runner.

        Two conditions, both about correctness rather than performance:

        * the profile must be a CONTAINER profile — a resident process on the
          runtime's own host would be a long-lived hole, which is what P6
          removed;
        * the kind must be declared poolable. Writes are excluded: the L2 profile
          carries a writable warehouse account, and a Pod that keeps one alive
          between tasks is exactly the coexistence Q5 forbids.
        """
        if not settings.warm_pool_enabled:
            return False
        if profile.level is not IsolationLevel.CONTAINER:
            return False
        return kind in settings.warm_pool_kinds

    async def acquire(
        self, workspace_path: str, environment: str, profile: IsolationProfile
    ) -> Optional[_PooledRunner]:
        """Return a ready runner for a working copy, or None when unavailable.

        None means "use the one-shot vehicle" — never "wait". A pool that queues
        turns a capacity limit into unbounded latency, and the one-shot path is
        always available.
        """
        async with self._lock:
            await self._retire_expired_locked()

            runner = self._runners.get(workspace_path)
            if runner is not None:
                return runner

            if len(self._runners) >= settings.warm_pool_max_pods:
                logger.info(
                    "warm_pool_full",
                    workspace_path=workspace_path,
                    pods=len(self._runners),
                    limit=settings.warm_pool_max_pods,
                    hint="falling back to a one-shot task",
                )
                return None

            try:
                runner = await self._start(workspace_path, environment, profile)
            except Exception as exc:  # noqa: BLE001 - fall back, never fail the task
                logger.warning(
                    "warm_pool_start_failed",
                    workspace_path=workspace_path,
                    error=str(exc),
                    hint="falling back to a one-shot task",
                )
                return None
            self._runners[workspace_path] = runner
            return runner

    async def _start(
        self, workspace_path: str, environment: str, profile: IsolationProfile
    ) -> _PooledRunner:
        """Create the Pod, wait for an address, connect and greet."""
        name = pool_pod_name(workspace_path)
        backend = self._backend()
        await asyncio.to_thread(
            self._create_pod, backend, name, workspace_path, environment, profile
        )
        host = await asyncio.wait_for(
            self._await_address(backend, name),
            timeout=settings.warm_pool_ready_timeout,
        )
        runner = _PooledRunner(name, workspace_path, host)
        try:
            await runner.connect()
        except Exception:
            await self._delete_pod(backend, name)
            raise
        logger.info(
            "warm_pool_runner_started",
            workspace_path=workspace_path,
            pod=name,
            host=host,
            kinds=len(runner.kinds),
        )
        return runner

    def _create_pod(
        self,
        backend: Any,
        name: str,
        workspace_path: str,
        environment: str,
        profile: IsolationProfile,
    ) -> None:
        """Build and create the resident runner Pod (blocking; call in a thread).

        Mount, scheduling, resources and security context all come from the task
        backend's helpers: a resident runner and a one-shot task have IDENTICAL
        isolation requirements, and a second copy of that construction would be a
        second place for T1 to regress.
        """
        api = backend._api  # noqa: SLF001 - deliberate reuse, see the docstring
        volumes, mounts = backend._volumes_for_path(workspace_path, name)  # noqa: SLF001
        scheduling = backend._scheduling(profile)  # noqa: SLF001

        container = api.V1Container(
            name="runner",
            image=settings.task_runner_image,
            image_pull_policy=settings.task_runner_image_pull_policy,
            command=["python", "-m", "src.main", "--serve", str(RUNNER_PORT)],
            env=[
                # The CONTAINER's view of the copy, not the runtime-side path:
                # the resident runner refuses requests naming anything else.
                api.V1EnvVar(
                    name="DATACODE_WORKSPACE_PATH",
                    value=_mount_path(),
                ),
                # Writable, ignored, hidden: see the one-shot backend. The
                # runner re-pins it per task from the spec as well, so a Pod
                # started by hand behaves like one started here.
                api.V1EnvVar(name="HOME", value=tool_home(_mount_path())),
                api.V1EnvVar(name="DATACODE_ENVIRONMENT", value=environment),
                api.V1EnvVar(name="TZ", value="UTC"),
            ],
            volume_mounts=mounts,
            resources=backend._resources(profile),  # noqa: SLF001
            security_context=backend._container_security_context(profile),  # noqa: SLF001
            ports=[api.V1ContainerPort(name="runner", container_port=RUNNER_PORT)],
        )

        pod_spec_kwargs: dict = {}
        dns_policy = str(profile.settings.get("dns_policy") or "")
        if dns_policy:
            pod_spec_kwargs["dns_policy"] = dns_policy
            dns_config = backend._dns_config()  # noqa: SLF001
            if dns_policy == "None" and dns_config is not None:
                pod_spec_kwargs["dns_config"] = dns_config
            elif dns_policy == "None":
                pod_spec_kwargs["dns_policy"] = "ClusterFirst"

        pod = api.V1Pod(
            metadata=api.V1ObjectMeta(
                name=name,
                namespace=settings.task_namespace,
                labels={
                    # The same `app` label the one-shot tasks carry, so this Pod
                    # inherits the identical EGRESS posture, plus a kind label
                    # that separates the two policies: a resident runner needs
                    # one inbound path (the runtime dials it), which
                    # task-runner-default-deny must keep denying for one-shot
                    # tasks. See warm-pool-policy in deploy/egress.
                    "app": "task-runner",
                    "datacode.io/kind": "warm-pool",
                    "datacode.io/workspace": Path(workspace_path).name,
                },
            ),
            spec=api.V1PodSpec(
                restart_policy="Never",
                service_account_name=settings.task_service_account,
                automount_service_account_token=False,
                enable_service_links=False,
                host_network=False,
                host_pid=False,
                host_ipc=False,
                security_context=backend._pod_security_context(profile),  # noqa: SLF001
                node_selector=scheduling.get("node_selector") or None,
                tolerations=scheduling.get("tolerations"),
                containers=[container],
                volumes=volumes,
                **pod_spec_kwargs,
            ),
        )
        backend._core.create_namespaced_pod(settings.task_namespace, pod)  # noqa: SLF001

    async def _await_address(self, backend: Any, pod_name: str) -> str:
        """Poll the Pod until it has a routable address."""
        while True:
            pod = await asyncio.to_thread(
                backend._core.read_namespaced_pod,  # noqa: SLF001
                pod_name,
                settings.task_namespace,
            )
            phase = pod.status.phase if pod.status else ""
            if phase == "Failed":
                raise TaskBackendError(f"warm runner Pod {pod_name} failed to start")
            pod_ip = (pod.status.pod_ip if pod.status else None) or ""
            if pod_ip:
                return pod_ip
            await asyncio.sleep(0.5)

    # ── execution ─────────────────────────────────────────────────────

    async def run(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: Optional[SandboxCredentials] = None,
    ) -> Optional[TaskResult]:
        """Run a task on a resident runner, or return None to fall back.

        None is returned for every condition that makes the WARM path
        unavailable — pool full, Pod busy, image missing the kind, transport
        broken — so the caller's fallback is one branch rather than a set of
        exception types. A task that RAN and failed comes back as a failed
        TaskResult, never as None: that is an outcome, not an unavailability.
        """
        if not self.poolable(spec.kind, profile):
            return None

        runner = await self.acquire(spec.workspace_path, spec.environment, profile)
        if runner is None:
            return None
        if not runner.implements(spec.kind):
            logger.warning(
                "warm_pool_kind_unsupported",
                pod=runner.pod_name,
                kind=spec.kind,
                hint="the runner image is older than this runtime; using a one-shot task",
            )
            return None

        try:
            response = await runner.run(spec, credentials)
        except (asyncio.TimeoutError, OSError, ProtocolError, TaskBackendError) as exc:
            # The transport failed, so it is unknown whether the task ran. The
            # Pod is retired (its state is no longer trusted) and the caller
            # falls back — a one-shot retry is safe for the poolable kinds,
            # which are read-only by construction.
            logger.warning(
                "warm_pool_transport_failed",
                pod=runner.pod_name,
                task_id=spec.task_id,
                error=str(exc),
            )
            await self.retire(spec.workspace_path)
            return None

        error = (response.get("error") or {}) if not response.get("ok") else {}
        if error.get("code") == ERR_BUSY:
            # Another task holds the Pod. Not an error: the caller uses a
            # one-shot vehicle, which is what provides burst capacity.
            logger.info("warm_pool_busy", pod=runner.pod_name, task_id=spec.task_id)
            return None

        result = result_from_response(spec.task_id, response)
        if runner.tasks_served >= settings.warm_pool_max_tasks:
            # Retired AFTER returning the result: a task must never fail because
            # its Pod reached the end of its service life.
            logger.info(
                "warm_pool_runner_retiring",
                pod=runner.pod_name,
                tasks_served=runner.tasks_served,
            )
            await self.retire(spec.workspace_path)
        return result

    async def ping(self, workspace_path: str) -> bool:
        """Whether the copy's runner answers. Used by health and by tests."""
        runner = self._runners.get(workspace_path)
        if runner is None:
            return False
        try:
            async with runner._lock:  # noqa: SLF001
                runner._next_id += 1  # noqa: SLF001
                runner._writer.write(  # noqa: SLF001
                    encode({"v": 1, "id": runner._next_id, "op": OP_PING})  # noqa: SLF001
                )
                await runner._writer.drain()  # noqa: SLF001
                response = await asyncio.wait_for(
                    read_frame(runner._reader), timeout=10  # noqa: SLF001
                )
            return bool(response.get("ok"))
        except Exception:  # noqa: BLE001 - unreachable means not alive
            return False

    # ── retirement ────────────────────────────────────────────────────

    async def retire(self, workspace_path: str) -> bool:
        """Close and delete a copy's runner. Idempotent."""
        runner = self._runners.pop(workspace_path, None)
        if runner is None:
            return False
        await runner.close()
        await self._delete_pod(self._backend(), runner.pod_name)
        logger.info(
            "warm_pool_runner_retired",
            workspace_path=workspace_path,
            pod=runner.pod_name,
            tasks_served=runner.tasks_served,
        )
        return True

    async def invalidate(self, workspace_path: str) -> None:
        """Retire the runner of a copy whose contents changed underneath it.

        Called from the same points that invalidate the warm sandbox (a git
        switch, a completed apply): the resident runner holds no Context, but it
        does hold a working directory and a SQLMesh cache built from the old
        tree.
        """
        await self.retire(workspace_path)

    async def _retire_expired_locked(self) -> None:
        """Retire idle runners. Caller holds the pool lock."""
        cutoff = settings.warm_pool_idle_seconds
        expired = [
            path
            for path, runner in self._runners.items()
            if runner.idle_seconds > cutoff
        ]
        for path in expired:
            runner = self._runners.pop(path)
            await runner.close()
            await self._delete_pod(self._backend(), runner.pod_name)
            logger.info(
                "warm_pool_runner_expired",
                workspace_path=path,
                pod=runner.pod_name,
                idle_seconds=int(runner.idle_seconds),
            )

    async def _delete_pod(self, backend: Any, pod_name: str) -> None:
        try:
            await asyncio.to_thread(
                backend._core.delete_namespaced_pod,  # noqa: SLF001
                pod_name,
                settings.task_namespace,
            )
        except Exception as exc:  # noqa: BLE001 - best effort, like every teardown
            logger.warning(
                "warm_pool_pod_delete_failed",
                pod=pod_name,
                error=str(exc),
                hint="delete it manually if it persists",
            )

    async def shutdown(self) -> None:
        """Retire every runner (service shutdown)."""
        for path in list(self._runners):
            await self.retire(path)

    def stats(self) -> dict[str, Any]:
        """Pool state for /health."""
        return {
            "enabled": settings.warm_pool_enabled,
            "pods": len(self._runners),
            "max_pods": settings.warm_pool_max_pods,
            "runners": [
                {
                    "pod": runner.pod_name,
                    "tasks_served": runner.tasks_served,
                    "idle_seconds": int(runner.idle_seconds),
                }
                for runner in self._runners.values()
            ],
        }


def _mount_path() -> str:
    """Where the working copy is mounted inside a task container."""
    from src.tasks.backends.kubernetes import WORKSPACE_MOUNT_PATH

    return WORKSPACE_MOUNT_PATH


_pool: Optional[WarmPool] = None


def get_warm_pool() -> WarmPool:
    """The process-wide pool."""
    global _pool
    if _pool is None:
        _pool = WarmPool()
    return _pool


async def shutdown_warm_pool() -> None:
    """Retire every resident runner. Called from the app lifespan."""
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None


__all__ = [
    "ERR_SUBMIT_FAILED",
    "RUNNER_PORT",
    "WarmPool",
    "get_warm_pool",
    "pool_pod_name",
    "shutdown_warm_pool",
]
