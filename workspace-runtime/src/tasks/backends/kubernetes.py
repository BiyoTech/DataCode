"""Kubernetes task backend — one Job per task (design 04 §3, design 06 P6a).

The production vehicle, and the only one that can provide L2/L3-base: those
levels ARE the container, so nothing here is optional decoration. Every field of
the Pod spec below traces to a threat in design 04 §1.2, and the profile is the
source of every value — this module translates, it does not decide.

Working-copy hand-off (design 04 §3.3, strategy S1)
---------------------------------------------------
A task must see exactly ONE working copy; that is what makes cross-tenant reads
(T1) impossible by construction rather than by path validation. The copy exists
on the node holding its lease, so the Job is pinned to THIS replica's node and
mounts only that copy's sub-path:

* ``mount_kind: instance_local`` (production) — the replica's ReadWriteOnce PVC.
  RWO permits several pods on one node, which is precisely why co-scheduling is
  required rather than merely convenient.
* ``mount_kind: ephemeral`` / hostPath — the node-level working directory.

Neither introduces a new storage mode: the mount strategy follows the medium the
deployment already declared (ADR-003 / Q7).

Honest limitation under Q2 (no sandboxed kernel): the mount is enforced by the
container runtime alone. A container escape reaches other working copies on the
node. The mitigations are the dedicated user-code node pool and the kernel patch
baseline; L3-strong is what would close it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from datacode_common.isolation import IsolationLevel, IsolationProfile
from datacode_common.python_deps import TASK_KIND_DEPS_SYNC, site_packages_path
from datacode_common.task_protocol import (
    ERR_NO_RESULT,
    ERR_SANDBOX_OOM,
    ERR_TIMEOUT,
    TaskResult,
    TaskSpec,
    TaskStatus,
    extract_result,
)
from datacode_common.workspace_layout import tool_home

from src.config import settings
from src.deps.manifest import overlay_for_injection
from src.tasks.backends.base import TaskBackend, TaskBackendError
from src.tasks.credentials import SandboxCredentials
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Where the vehicle places the spec and the working copy inside the container.
SPEC_MOUNT_PATH = "/task"
SPEC_FILE_PATH = f"{SPEC_MOUNT_PATH}/spec.json"
WORKSPACE_MOUNT_PATH = "/workspace"


def container_spec_json(spec: TaskSpec) -> str:
    """Serialize a spec with the working-copy path REWRITTEN for the container.

    The runtime-side path (e.g. /data/workspaces/...) does not exist inside the
    container, where the copy is mounted at /workspace; the runner would look
    for a directory that is simply not there. Everything else about the spec is
    carried unchanged — a separate module-level function (not a method) so the
    rewrite is testable without a cluster client, and so the warm-sandbox
    backend can document that it does the SAME thing via its env var.
    """
    import dataclasses

    return dataclasses.replace(spec, workspace_path=WORKSPACE_MOUNT_PATH).to_json()

# How often the Job's status is polled. Tasks run for seconds to an hour, so a
# short interval only adds API load; the runner streams progress independently.
_POLL_INTERVAL_SECONDS = 2.0

# Labels every created object carries, so a leak is findable and a tenant's
# tasks can be listed or purged without guessing names. Public because they are
# a CONTRACT, not an implementation detail: the NetworkPolicies in
# deploy/egress/ select on them, and the sandbox inventory (which the reconciler
# reads to find orphaned Pods) must query by exactly the same keys.
LABEL_APP = "app"
# Which vehicle a Pod is. NetworkPolicies select on it: a one-shot task takes no
# inbound traffic at all, while a warm-pool runner takes exactly one path (the
# runtime dialling it), and those cannot be the same rule.
LABEL_VEHICLE = "datacode.io/kind"
LABEL_TENANT = "datacode/tenant-id"
LABEL_TASK = "datacode/task-id"
LABEL_KIND = "datacode/task-kind"
LABEL_ISOLATION = "datacode/isolation"

# Value of LABEL_APP on every task-runner object.
APP_TASK_RUNNER = "task-runner"
# Value of LABEL_VEHICLE on a one-shot task Pod.
VEHICLE_ONE_SHOT = "one-shot"


class KubernetesBackend(TaskBackend):
    """Creates a Job per task, reads its result from the Pod's logs."""

    def __init__(self) -> None:
        self._api: Any = None
        self._batch: Any = None
        self._core: Any = None
        self._load_client()
        self._validate_configuration()

    # ── setup ──────────────────────────────────────────────────────────

    def _load_client(self) -> None:
        """Import and configure the Kubernetes client.

        An optional dependency: dev installs do not carry it. A missing library
        combined with ``task_backend: kubernetes`` is a misconfiguration and must
        fail at startup, not on the first task.
        """
        try:
            from kubernetes import client, config  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TaskBackendError(
                "task_backend=kubernetes requires the kubernetes client; "
                "install workspace-runtime with the [k8s] extra"
            ) from exc

        try:
            # In-cluster first: that is how the runtime actually runs. The
            # kubeconfig fallback exists for operators debugging from a laptop.
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 — any failure means "not in a cluster"
            try:
                config.load_kube_config()
            except Exception as exc:  # noqa: BLE001
                raise TaskBackendError(
                    f"no usable Kubernetes configuration: {exc}"
                ) from exc

        self._batch = client.BatchV1Api()
        self._core = client.CoreV1Api()
        self._api = client

    def _validate_configuration(self) -> None:
        """Refuse to start with a configuration that cannot place a task.

        Each of these would otherwise surface as a Job that never schedules, or
        worse, one that schedules on a platform node.
        """
        if not settings.task_runner_image:
            raise TaskBackendError("task_runner_image is required")
        if not settings.task_node_name:
            raise TaskBackendError(
                "task_node_name is required: the working copy exists on THIS "
                "replica's node, so the runner must be co-scheduled there "
                "(inject spec.nodeName via the downward API)"
            )
        if not settings.task_workcopy_pvc and not settings.task_host_workdir:
            raise TaskBackendError(
                "no working-copy source: set task_workcopy_pvc "
                "(mount_kind: instance_local) or task_host_workdir "
                "(mount_kind: ephemeral/hostPath)"
            )

    @property
    def name(self) -> str:
        return "kubernetes"

    @property
    def supported_levels(self) -> frozenset[IsolationLevel]:
        return frozenset({IsolationLevel.CONTAINER})

    # ── execution ──────────────────────────────────────────────────────

    async def execute(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: Optional[SandboxCredentials] = None,
    ) -> TaskResult:
        """Create the Job, wait for it, read the result, then clean up."""
        job_name = f"task-{spec.task_id}"
        namespace = settings.task_namespace

        try:
            await asyncio.to_thread(self._create_spec_configmap, spec, job_name)
            if credentials is not None and credentials.env:
                await asyncio.to_thread(
                    self._create_credential_secret, spec, credentials, job_name
                )
            await asyncio.to_thread(
                self._create_job, spec, profile, job_name, credentials
            )
        except TaskBackendError:
            await self._cleanup(job_name, namespace)
            raise
        except Exception as exc:  # noqa: BLE001 — client raises many shapes
            await self._cleanup(job_name, namespace)
            raise TaskBackendError(
                f"could not create the task Job {job_name}: {exc}"
            ) from exc

        logger.info(
            "task_k8s_submitted",
            task_id=spec.task_id,
            job=job_name,
            kind=spec.kind,
            profile=profile.name,
            node=settings.task_node_name,
            timeout_s=spec.timeout_seconds,
        )

        try:
            timed_out = await self._wait_for_completion(job_name, spec.timeout_seconds)
            logs = await asyncio.to_thread(self._read_pod_logs, job_name)
            oom_killed = await asyncio.to_thread(self._pod_oom_killed, job_name)
            return self._interpret(spec, logs, timed_out=timed_out, oom_killed=oom_killed)
        finally:
            # Always: a leaked Job holds a node slot and its ConfigMap outlives
            # the task that needed it.
            await self._cleanup(job_name, namespace)

    def _interpret(
        self, spec: TaskSpec, logs: str, *, timed_out: bool, oom_killed: bool = False
    ) -> TaskResult:
        """Prefer the runner's result; fall back to the vehicle's own evidence.

        ``oom_killed`` comes from the Pod's termination reason — the one
        authoritative source for "the memory limit killed it". Claiming OOM on
        the strength of an absent result line (the old "(OOMKilled or
        evicted?)" guess) would mislabel evictions and API hiccups as the
        user's memory problem.
        """
        result = extract_result(logs.splitlines())
        if result is not None:
            logger.info(
                "task_k8s_finished",
                task_id=spec.task_id,
                status=result.status.value,
                duration_ms=result.duration_ms,
                error_code=result.error_code or None,
            )
            return result

        tail = logs[-settings.task_log_tail_bytes :]
        if timed_out:
            return TaskResult.failure(
                spec.task_id,
                ERR_TIMEOUT,
                f"task exceeded its {spec.timeout_seconds}s budget; "
                f"the Job was deleted. Log tail: {tail[-2048:] or '(empty)'}",
                status=TaskStatus.TIMED_OUT,
            )

        if oom_killed:
            # The kernel's verdict, read from the Pod status: the task never
            # got to report, and the reason is the memory limit. The contract
            # maps this to SANDBOX_OOM (507) — an actionable "raise the
            # limit", not an opaque internal error.
            return TaskResult.failure(
                spec.task_id,
                ERR_SANDBOX_OOM,
                "the task was killed by its container's memory limit",
            )

        logger.error("task_k8s_no_result", task_id=spec.task_id, log_tail=tail[-2048:])
        return TaskResult.failure(
            spec.task_id,
            ERR_NO_RESULT,
            "the task Pod finished without reporting a result and without a "
            f"classifiable termination reason. Log tail: {tail[-2048:] or '(empty)'}",
        )

    def _pod_oom_killed(self, job_name: str) -> bool:
        """Whether the Job's Pod terminated with reason ``OOMKilled``.

        Reads the Pod status rather than guessing from the absence of a result
        line. Unreadable status answers False — an OOM claim the platform
        cannot back with evidence must not be made (the honest fallback is
        ERR_NO_RESULT, which carries no verdict).
        """
        try:
            pods = self._core.list_namespaced_pod(
                settings.task_namespace, label_selector=f"job-name={job_name}"
            )
            for pod in pods.items:
                for entry in list(
                    getattr(pod.status, "container_statuses", None) or []
                ):
                    for state in (
                        getattr(entry, "last_state", None),
                        getattr(entry, "state", None),
                    ):
                        terminated = getattr(state, "terminated", None)
                        if (
                            terminated is not None
                            and getattr(terminated, "reason", "") == "OOMKilled"
                        ):
                            return True
            return False
        except Exception as exc:  # noqa: BLE001 — ApiException and transport errors
            logger.warning(
                "task_k8s_pod_status_unavailable",
                job=job_name,
                error=str(exc),
                effect="an OOM kill cannot be confirmed; reporting no verdict",
            )
            return False

    async def _wait_for_completion(self, job_name: str, timeout_seconds: int) -> bool:
        """Poll until the Job finishes. Returns True if the deadline hit first.

        The Job also carries ``activeDeadlineSeconds``, but that only kills the
        Pod. Polling with our own bound means a stuck API server cannot make this
        call hang forever either.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds + _POLL_INTERVAL_SECONDS
        while True:
            status = await asyncio.to_thread(self._job_status, job_name)
            if status in {"succeeded", "failed"}:
                return False
            if asyncio.get_running_loop().time() >= deadline:
                return True
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    # ── Kubernetes object construction ─────────────────────────────────

    def _labels(self, spec: TaskSpec, profile_name: str = "") -> dict[str, str]:
        labels = {
            LABEL_APP: APP_TASK_RUNNER,
            # Vehicle kind, and it is load-bearing: `task-runner-default-deny`
            # selects `one-shot` so it can keep denying ALL inbound traffic,
            # while warm-pool runners (which the runtime dials) are governed by
            # `warm-pool-policy`. Dropping this label would leave a task Pod
            # matched by no policy at all — i.e. with unrestricted egress.
            LABEL_VEHICLE: VEHICLE_ONE_SHOT,
            LABEL_TASK: spec.task_id,
            LABEL_KIND: spec.kind.replace(".", "-"),
        }
        if spec.tenant_id:
            labels[LABEL_TENANT] = spec.tenant_id
        if profile_name:
            labels[LABEL_ISOLATION] = profile_name
        return labels

    def _create_spec_configmap(self, spec: TaskSpec, job_name: str) -> None:
        """Store the spec as a ConfigMap the Pod mounts.

        A ConfigMap and not a Secret, deliberately: the spec must contain no
        credentials (``TaskSpec.validate`` enforces that), and using a Secret
        would signal the opposite to whoever reads this next.
        """
        body = self._api.V1ConfigMap(
            metadata=self._api.V1ObjectMeta(
                name=job_name,
                namespace=settings.task_namespace,
                labels=self._labels(spec),
            ),
            data={"spec.json": container_spec_json(spec)},
        )
        self._core.create_namespaced_config_map(settings.task_namespace, body)

    def _create_credential_secret(
        self, spec: TaskSpec, credentials: SandboxCredentials, job_name: str
    ) -> None:
        """Store the task's credentials in a Secret it consumes as environment.

        A Secret and not a ConfigMap: this holds the task token and the proxy URL
        that embeds it. It is created per task and deleted with the task, so there
        is no long-lived object to rotate — the credential's lifetime IS the task's.

        The Pod mounts it via ``envFrom``, not as a volume, so nothing lands on a
        filesystem the task could copy it out of. Kubernetes keeps Secret volumes
        in tmpfs, but an env var is not written anywhere at all, which is stricter.
        """
        body = self._api.V1Secret(
            metadata=self._api.V1ObjectMeta(
                name=job_name,
                namespace=settings.task_namespace,
                labels=self._labels(spec),
            ),
            type="Opaque",
            string_data=dict(credentials.env),
        )
        self._core.create_namespaced_secret(settings.task_namespace, body)
        logger.info(
            "task_k8s_credentials_created",
            task_id=spec.task_id,
            secret=job_name,
            # Key names only. The values are the credential.
            **credentials.safe_summary(),
        )

    def _volumes(self, spec: TaskSpec, job_name: str) -> tuple[list, list]:
        """Build the volumes and mounts for one working copy plus scratch.

        ``sub_path`` is what narrows a whole volume down to a single working
        copy. Without it the container would see every copy on the medium, and
        T1 would be back.
        """
        volumes = [
            self._api.V1Volume(
                name="task-spec",
                config_map=self._api.V1ConfigMapVolumeSource(name=job_name),
            ),
        ]
        mounts = [
            self._api.V1VolumeMount(
                name="task-spec", mount_path=SPEC_MOUNT_PATH, read_only=True
            ),
        ]
        extra_volumes, extra_mounts = self._volumes_for_path(
            spec.workspace_path, job_name
        )
        return volumes + extra_volumes, mounts + extra_mounts

    def _volumes_for_path(self, workspace_path: str, name: str) -> tuple[list, list]:
        """Working-copy mount plus memory-backed scratch, for one path.

        Shared with the warm-sandbox backend: a task Pod and a warm sandbox Pod
        have IDENTICAL hand-off constraints (single-copy subPath, same node as
        the lease holder), and a second copy of that logic would be a second
        place for T1 to regress.
        """
        del name  # symmetry with future per-object volumes; unused today
        volumes = [
            # Memory-backed scratch: nothing written here reaches a disk, and
            # it disappears with the Pod.
            self._api.V1Volume(
                name="scratch",
                empty_dir=self._api.V1EmptyDirVolumeSource(
                    medium="Memory", size_limit="2Gi"
                ),
            ),
        ]
        mounts = [
            self._api.V1VolumeMount(name="scratch", mount_path="/tmp"),  # noqa: S108
        ]

        sub_path = self._workcopy_sub_path(workspace_path)
        if settings.task_workcopy_pvc:
            volumes.append(
                self._api.V1Volume(
                    name="workcopy",
                    persistent_volume_claim=(
                        self._api.V1PersistentVolumeClaimVolumeSource(
                            claim_name=settings.task_workcopy_pvc
                        )
                    ),
                )
            )
        else:
            volumes.append(
                self._api.V1Volume(
                    name="workcopy",
                    host_path=self._api.V1HostPathVolumeSource(
                        path=settings.task_host_workdir, type="Directory"
                    ),
                )
            )
        mounts.append(
            self._api.V1VolumeMount(
                name="workcopy",
                mount_path=WORKSPACE_MOUNT_PATH,
                sub_path=sub_path,
            )
        )
        return volumes, mounts

    @staticmethod
    def _overlay_env(spec: TaskSpec) -> str:
        """``PYTHONPATH`` for the Job (injection point 3 of 3), or ``""``.

        The DECISION is made against the runtime's own view of the copy (that is
        where the manifest is readable), while the VALUE must be the container's
        view — the copy is mounted at :data:`WORKSPACE_MOUNT_PATH`, so a
        runtime-side path here would name a directory that does not exist in the
        Pod. Getting these two the wrong way round is silent: the variable is set,
        nothing is importable, and the error surfaces inside user code.

        An install task gets no overlay: it is the process that builds that tree.
        """
        if spec.kind == TASK_KIND_DEPS_SYNC:
            return ""
        if not overlay_for_injection(spec.workspace_path, consumer="sandbox_job"):
            return ""
        return site_packages_path(WORKSPACE_MOUNT_PATH)

    @staticmethod
    def _workcopy_sub_path(workspace_path: str) -> str:
        """Return the working copy's path RELATIVE to the storage root.

        Derived from the absolute path the runtime already resolved, so both
        sides agree on one layout. A path outside the root is refused rather than
        clamped: mounting the wrong sub-tree is exactly the failure this whole
        mechanism exists to prevent.
        """
        root = Path(settings.materialize_root or settings.workspace_base_path).resolve()
        target = Path(workspace_path).resolve()
        try:
            return str(target.relative_to(root))
        except ValueError as exc:
            raise TaskBackendError(
                f"working copy {workspace_path} is not inside the storage "
                f"root {root}; refusing to guess a mount sub-path"
            ) from exc

    def _pod_security_context(self, profile: IsolationProfile):
        run_as = int(profile.settings.get("run_as_user", 65534))
        return self._api.V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=run_as,
            run_as_group=run_as,
            # The working copy is group-owned by the runtime's fsGroup; matching
            # it is what lets the task write its own copy and nothing else.
            fs_group=run_as,
            seccomp_profile=self._api.V1SeccompProfile(
                type=str(profile.settings.get("seccomp", "RuntimeDefault"))
            ),
        )

    def _container_security_context(self, profile: IsolationProfile):
        return self._api.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=bool(
                profile.settings.get("read_only_root_fs", True)
            ),
            capabilities=self._api.V1Capabilities(
                drop=list(profile.settings.get("drop_capabilities") or ["ALL"])
            ),
        )

    def _resources(self, profile: IsolationProfile):
        declared = profile.settings.get("resources") or {}
        limits = {
            "cpu": str(declared.get("cpu_limit", "2")),
            "memory": str(declared.get("memory_limit", "4Gi")),
            "ephemeral-storage": str(
                declared.get("ephemeral_storage_limit", "8Gi")
            ),
        }
        # Requests below limits so a node can host several idle runners, while
        # the limit is what actually bounds a runaway task (T5).
        requests = {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"}
        return self._api.V1ResourceRequirements(limits=limits, requests=requests)

    def _scheduling(self, profile: IsolationProfile) -> dict[str, Any]:
        """Node placement: the lease's node, and the user-code pool if declared."""
        selector: dict[str, str] = {"kubernetes.io/hostname": settings.task_node_name}
        tolerations = []
        node_pool = str(profile.settings.get("node_pool") or "")
        if node_pool:
            selector["datacode.io/workload"] = node_pool
            tolerations.append(
                self._api.V1Toleration(
                    key="datacode.io/user-code", operator="Exists", effect="NoSchedule"
                )
            )
        return {"node_selector": selector, "tolerations": tolerations or None}

    def _dns_config(self):
        """Resolver for a Pod with ``dnsPolicy: None``.

        Points at the controlled resolver, which answers only for the destinations
        a tenant declared. That matters beyond tidiness: with the cluster's own
        resolver a sandbox could enumerate internal services by name, and the
        cluster search domains would make ``postgres`` alone resolve.

        Falls back to the cluster resolver when none is configured, and says so
        loudly — a Pod with no resolver at all fails every connection, which looks
        like a network bug rather than a missing setting.
        """
        resolver = (settings.task_dns_server or "").strip()
        if not resolver:
            logger.warning(
                "task_k8s_dns_resolver_unset",
                hint="set task_dns_server to the egress-dns service address; "
                "falling back to ClusterFirst, which lets a sandbox resolve "
                "internal service names",
            )
            return None
        return self._api.V1PodDNSConfig(
            nameservers=[resolver],
            # No search domains: a bare "postgres" must not become
            # "postgres.datacode.svc.cluster.local".
            searches=[],
            options=[self._api.V1PodDNSConfigOption(name="ndots", value="1")],
        )

    def _create_job(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        job_name: str,
        credentials: Optional[SandboxCredentials] = None,
    ) -> None:
        """Create the Job. Every hardening field below comes from the profile."""
        volumes, mounts = self._volumes(spec, job_name)
        scheduling = self._scheduling(profile)
        env = [
            self._api.V1EnvVar(name="DATACODE_TASK_ID", value=spec.task_id),
            self._api.V1EnvVar(name="DATACODE_TASK_SPEC_PATH", value=SPEC_FILE_PATH),
            # The image's HOME belongs to its unprivileged uid and points at a
            # path that does not exist on a read-only root filesystem. Pointing
            # it into the platform's own state directory inside the mounted copy
            # gives every tool a writable home that is ignored by Git and hidden
            # from the explorer.
            self._api.V1EnvVar(name="HOME", value=tool_home(WORKSPACE_MOUNT_PATH)),
            self._api.V1EnvVar(name="TZ", value="UTC"),
        ]
        overlay = self._overlay_env(spec)
        if overlay:
            env.append(self._api.V1EnvVar(name="PYTHONPATH", value=overlay))

        container = self._api.V1Container(
            name="runner",
            image=settings.task_runner_image,
            image_pull_policy=settings.task_runner_image_pull_policy,
            # The spec path is the ONLY input. No command override: the image's
            # entrypoint is the runner, so a Job cannot be repurposed to run
            # something else in this sandbox.
            args=[SPEC_FILE_PATH],
            env=env,
            # Credentials arrive as environment from the per-task Secret, so they
            # are never written to a filesystem the task could exfiltrate from.
            env_from=(
                [
                    self._api.V1EnvFromSource(
                        secret_ref=self._api.V1SecretEnvSource(name=job_name)
                    )
                ]
                if credentials is not None and credentials.env
                else None
            ),
            volume_mounts=mounts,
            resources=self._resources(profile),
            security_context=self._container_security_context(profile),
        )

        pod_spec_kwargs: dict[str, Any] = {
            "containers": [container],
            "volumes": volumes,
            "restart_policy": "Never",
            # No Kubernetes API access: the account has no bindings, and the
            # token is not even mounted.
            "service_account_name": settings.task_service_account,
            "automount_service_account_token": bool(
                profile.settings.get("automount_service_account_token", False)
            ),
            # Cluster service env vars would hand the sandbox the platform's own
            # addresses. This is the field whose absence has bitten others.
            "enable_service_links": bool(
                profile.settings.get("enable_service_links", False)
            ),
            "host_network": bool(profile.settings.get("host_network", False)),
            "host_pid": bool(profile.settings.get("host_pid", False)),
            "host_ipc": bool(profile.settings.get("host_ipc", False)),
            "security_context": self._pod_security_context(profile),
            # Hard stop for a runaway task, independent of our own polling.
            "active_deadline_seconds": spec.timeout_seconds,
            "node_selector": scheduling["node_selector"],
        }
        if scheduling["tolerations"]:
            pod_spec_kwargs["tolerations"] = scheduling["tolerations"]

        dns_policy = str(profile.settings.get("dns_policy") or "")
        if dns_policy:
            pod_spec_kwargs["dns_policy"] = dns_policy
            if dns_policy == "None":
                # dnsPolicy: None without a dnsConfig leaves the Pod with NO
                # resolver, so nothing resolves and every task fails at connect
                # time. The controlled resolver is what the profile means by it —
                # and when none is configured the policy is downgraded rather than
                # producing a Pod that cannot resolve anything.
                dns_config = self._dns_config()
                if dns_config is not None:
                    pod_spec_kwargs["dns_config"] = dns_config
                else:
                    pod_spec_kwargs["dns_policy"] = "ClusterFirst"

        runtime_class = profile.runtime_class
        if runtime_class and runtime_class != "runc":
            # Only set for a sandboxed kernel; "runc" is the cluster default and
            # naming it explicitly would fail on clusters without that class.
            pod_spec_kwargs["runtime_class_name"] = runtime_class

        job = self._api.V1Job(
            metadata=self._api.V1ObjectMeta(
                name=job_name,
                namespace=settings.task_namespace,
                labels=self._labels(spec, profile.name),
            ),
            spec=self._api.V1JobSpec(
                backoff_limit=0,  # a task is not idempotent; never retry silently
                ttl_seconds_after_finished=settings.task_job_ttl_seconds,
                template=self._api.V1PodTemplateSpec(
                    metadata=self._api.V1ObjectMeta(
                        labels=self._labels(spec, profile.name)
                    ),
                    spec=self._api.V1PodSpec(**pod_spec_kwargs),
                ),
            ),
        )
        self._batch.create_namespaced_job(settings.task_namespace, job)

    # ── status, logs, cleanup ──────────────────────────────────────────

    def _job_status(self, job_name: str) -> str:
        """Return ``succeeded`` / ``failed`` / ``running``.

        A vanished Job counts as failed: something outside this process removed
        it, and pretending it still runs would hang the caller.
        """
        try:
            job = self._batch.read_namespaced_job_status(
                job_name, settings.task_namespace
            )
        except Exception as exc:  # noqa: BLE001 — ApiException and transport errors
            logger.warning("task_k8s_status_unavailable", job=job_name, error=str(exc))
            return "failed"

        status = job.status
        if getattr(status, "succeeded", 0):
            return "succeeded"
        if getattr(status, "failed", 0):
            return "failed"
        return "running"

    def _read_pod_logs(self, job_name: str) -> str:
        """Read the logs of the Job's Pod — where the result line lives."""
        try:
            pods = self._core.list_namespaced_pod(
                settings.task_namespace, label_selector=f"job-name={job_name}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("task_k8s_pod_list_failed", job=job_name, error=str(exc))
            return ""

        chunks: list[str] = []
        for pod in pods.items:
            try:
                chunks.append(
                    self._core.read_namespaced_pod_log(
                        pod.metadata.name, settings.task_namespace
                    )
                    or ""
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "task_k8s_log_read_failed",
                    job=job_name,
                    pod=pod.metadata.name,
                    error=str(exc),
                )
        return "\n".join(chunks)

    async def _cleanup(self, job_name: str, namespace: str) -> None:
        """Delete the Job (and its Pods), the spec ConfigMap and the Secret.

        Best-effort and never raising: the task's outcome is already determined,
        and turning a cleanup hiccup into a task failure would misreport it.
        Kubernetes reaps finished Jobs on its own TTL as a backstop.

        The Secret matters most here: it holds the task token, and the credential's
        whole lifetime argument is that it dies with the task.
        """
        await asyncio.to_thread(self._delete_job, job_name, namespace)
        await asyncio.to_thread(self._delete_configmap, job_name, namespace)
        await asyncio.to_thread(self._delete_secret, job_name, namespace)

    def _delete_secret(self, name: str, namespace: str) -> None:
        try:
            self._core.delete_namespaced_secret(name, namespace)
        except Exception as exc:  # noqa: BLE001
            # Logged at ERROR, unlike the other two: a surviving Secret is a live
            # credential, not merely an orphaned object.
            logger.error(
                "task_k8s_secret_delete_failed",
                name=name,
                error=str(exc),
                hint="the task token remains valid until it expires on its own",
            )

    def _delete_job(self, job_name: str, namespace: str) -> None:
        try:
            self._batch.delete_namespaced_job(
                job_name,
                namespace,
                # Foreground: the Pods must go with the Job, or they keep the
                # working copy mounted.
                body=self._api.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_k8s_job_delete_failed", job=job_name, error=str(exc))

    def _delete_configmap(self, name: str, namespace: str) -> None:
        try:
            self._core.delete_namespaced_config_map(name, namespace)
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_k8s_configmap_delete_failed", name=name, error=str(exc))


__all__ = [
    "APP_TASK_RUNNER",
    "LABEL_APP",
    "LABEL_ISOLATION",
    "LABEL_KIND",
    "LABEL_TASK",
    "LABEL_TENANT",
    "LABEL_VEHICLE",
    "SPEC_FILE_PATH",
    "SPEC_MOUNT_PATH",
    "VEHICLE_ONE_SHOT",
    "WORKSPACE_MOUNT_PATH",
    "KubernetesBackend",
]
