"""Code location Pod vehicle — the container form of a Dagster code location.

Why this exists
---------------
A code location is the one place where the platform runs a user's Python not
as a one-shot task but as a LONG-LIVED server: it loads
``{workspace_path}/asset-dag/definitions.py`` and holds it in memory for the
orchestrator to query. Under the process vehicle that code ran inside
workspace-runtime itself — sharing its filesystem view, its network namespace
and its environment. That is exactly what P6 removes everywhere else, and a
code location is the most privileged user code there is: it outlives a single
request and is dialled by the orchestrator.

Pod mode gives a code location the same envelope a task sandbox gets: the
user-code node pool, the same security context and resource limits from the
``L3_base`` profile, and a working copy mounted at ``/workspace`` narrowed by
``subPath`` to this one environment.

The contract
------------
``deploy/k8s/dagster-container-plane.yaml`` carries the contract as a ConfigMap
(``codeloc-pod-contract``). The manifests built here are plain dicts rather
than Kubernetes client objects precisely so they can be diffed against that
YAML — in a test and in review — without a cluster client.

Discovery
---------
Each location gets a HEADLESS Service (``codeloc-<environment>``). The
Endpoints object Kubernetes maintains for it IS the registry: present means
dialable, absent means gone. There is no second source of truth to keep in
step, which is why the runtime does not persist Pod IPs anywhere.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# The gRPC port inside the Pod. Fixed, and the same for every location: each
# Pod has its own network namespace, so there is nothing to allocate — the
# port segmentation the process vehicle needs (src/codeloc/ports.py) simply
# does not apply here.
CODELOC_PORT = 4000

APP_CODE_LOCATION = "dagster-code-location"
LABEL_APP = "app"
LABEL_ENVIRONMENT = "datacode.io/environment"
LABEL_PLANE = "datacode.io/plane"
LABEL_LOCATION = "datacode.io/code-location"

# Where the working copy is mounted inside the Pod, and where Dagster expects
# to be started from so ``defs/`` resolves as the auto-discovery root.
WORKSPACE_MOUNT_PATH = "/workspace"
ASSET_DAG_SUBDIR = "asset-dag"

_VALID_NAME = re.compile(r"[^a-z0-9-]")


def _slug(value: str, limit: int = 32) -> str:
    """Lowercase, DNS-safe fragment of an arbitrary identifier."""
    return _VALID_NAME.sub("-", str(value).lower())[:limit].strip("-")


def pod_name(location_name: str) -> str:
    """Deterministic, DNS-safe Pod name for one code location.

    Deterministic so a runtime restart re-creates the SAME object name instead
    of stacking a second Pod beside a half-dead one.
    """
    return f"codeloc-{_slug(location_name)}-{hex(abs(hash(location_name)))[2:10]}"


def service_name(environment: str) -> str:
    """Headless Service name for an environment's code location."""
    return f"codeloc-{_slug(environment)}"


def service_fqdn(environment: str, namespace: Optional[str] = None) -> str:
    """Address the control plane and the runtime dial for a code location.

    The Service, not the Pod IP: an IP changes whenever the Pod is recreated,
    and advertising one would put a stale address into the orchestrator's
    workspace file on every respawn.
    """
    return f"{service_name(environment)}.{namespace or code_location_namespace()}.svc.cluster.local"


def code_location_namespace() -> str:
    """Namespace code location Pods are created in: the TASK plane.

    Not the platform namespace, and not a separate one:

    * a code location is user code, so it belongs where the other user-code
      vehicles are — that is where the ResourceQuota, the LimitRange, the
      user-code taint and the egress infrastructure already live;
    * reusing ``task_namespace`` keeps ONE answer to "where does user code
      run" instead of a second knob that can drift from the NetworkPolicies
      that guard it.

    Its policy is ``code-location-policy`` in
    ``deploy/egress/egress-plane.yaml``. A code location cannot reuse
    ``task-runner-default-deny``: that rule's premise is "no inbound
    connections", while a code location exists to be dialled.
    """
    return settings.task_namespace


def build_pod_manifest(
    *,
    name: str,
    namespace: str,
    location_name: str,
    environment: str,
    image: str,
    image_pull_policy: str,
    service_account: str,
    volumes: list[dict],
    volume_mounts: list[dict],
    scheduling: dict,
    security_context: dict,
    resources: dict,
    pod_security_context: dict,
    env: Optional[dict[str, str]] = None,
    dns: Optional[dict] = None,
) -> dict:
    """The code location Pod, as a plain dict (see the module docstring).

    Everything environment-specific (mounts, scheduling, security context,
    resources) is handed in already-shaped: those come from the isolation
    profile and the task backend, and this function's job is only to assemble
    what a CODE LOCATION specifically is.
    """
    environment_env = [
        {"name": key, "value": str(value)}
        for key, value in sorted((env or {}).items())
    ]
    spec: dict[str, Any] = {
        "serviceAccountName": service_account,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        # Never restarted: a location whose user code crashed must be replaced
        # by the manager (which reloads it and raises its revision), not
        # silently respawned behind the same identity.
        "restartPolicy": "Never",
        "securityContext": pod_security_context,
        "nodeSelector": scheduling.get("node_selector") or None,
        "tolerations": scheduling.get("tolerations"),
        "containers": [
            {
                "name": "code-location",
                "image": image,
                "imagePullPolicy": image_pull_policy,
                "command": [
                    "dagster", "api", "grpc",
                    "-h", "0.0.0.0",
                    "-p", str(CODELOC_PORT),
                ],
                "workingDir": f"{WORKSPACE_MOUNT_PATH}/{ASSET_DAG_SUBDIR}",
                "env": environment_env,
                "ports": [{"name": "grpc", "containerPort": CODELOC_PORT}],
                "securityContext": security_context,
                "resources": resources,
                "volumeMounts": volume_mounts,
            }
        ],
        "volumes": volumes,
    }
    if dns:
        spec.update(dns)

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                LABEL_APP: APP_CODE_LOCATION,
                LABEL_PLANE: "user-code",
                LABEL_ENVIRONMENT: environment,
                LABEL_LOCATION: _slug(location_name, 63),
            },
        },
        "spec": spec,
    }


def build_service_manifest(
    *, name: str, namespace: str, environment: str
) -> dict:
    """The headless Service whose Endpoints are the discovery registry."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                LABEL_APP: APP_CODE_LOCATION,
                LABEL_PLANE: "user-code",
                LABEL_ENVIRONMENT: environment,
            },
        },
        "spec": {
            "clusterIP": "None",
            "selector": {
                LABEL_APP: APP_CODE_LOCATION,
                LABEL_ENVIRONMENT: environment,
            },
            "ports": [{"name": "grpc", "port": CODELOC_PORT}],
        },
    }


class CodeLocationPodError(RuntimeError):
    """A code location Pod could not be created or reached.

    Raised rather than degraded: silently falling back to the process vehicle
    would make "pod mode" mean "process mode with extra steps", and the whole
    point of the mode is that user code does not run in here.
    """


def assert_vehicle_supported(isolation) -> None:
    """Fail when pod mode cannot be honoured, BEFORE anything runs.

    Called at startup (``main.lifespan``) so a vehicle misconfiguration fails
    the rollout, where the operator is present — not the first user's first
    deployment. Mirrors the guard inside :meth:`CodeLocationPodBackend.start`
    as a separate function because the preflight must not need the Kubernetes
    client that the backend's construction pulls in.
    """
    from datacode_common.isolation import IsolationLevel, NoIsolationAvailable

    try:
        profile = isolation.profile_for("dagster.codelocation")
    except NoIsolationAvailable as exc:
        raise CodeLocationPodError(
            "dagster.code_location_mode=pod but the isolation plane is "
            f"unavailable: {exc}"
        ) from exc
    if profile.level is not IsolationLevel.CONTAINER:
        raise CodeLocationPodError(
            "dagster.code_location_mode=pod but no container profile is "
            f"available (resolved {profile.name}/{profile.level.value}); "
            "set dagster.code_location_mode=process or enable the container plane"
        )


class CodeLocationPodBackend:
    """Creates, watches and deletes code location Pods.

    Reuses the task backend's cluster client AND its mount/scheduling/hardening
    helpers: the working-copy hand-off constraints (subPath narrowing, the
    lease holder's node) and the hardening are identical for a code location
    and a one-shot task, and a second copy is a second place for T1 to regress.
    """

    def __init__(self) -> None:
        from src.tasks.backends.kubernetes import KubernetesBackend

        self._tasks = KubernetesBackend()
        self._core = self._tasks._core
        self._api = self._tasks._api

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(
        self, spec_location: str, workspace_path: str, environment: str,
        env: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, int]:
        """Create the Pod and its Service; return ``(pod_name, host, port)``.

        The host returned is the Service FQDN: stable across Pod recreation,
        which is what makes it safe to advertise to the control plane.
        """
        from datacode_common.isolation import IsolationLevel

        from src.isolation.policy import runtime_isolation_policy

        profile = runtime_isolation_policy().profile_for("dagster.codelocation")
        if profile.level is not IsolationLevel.CONTAINER:
            raise CodeLocationPodError(
                "dagster.code_location_mode=pod but no container profile is "
                f"available (resolved {profile.name}/{profile.level.value}); "
                "set dagster.code_location_mode=process or enable the container plane"
            )

        name = pod_name(spec_location)
        service = service_name(environment)
        namespace = code_location_namespace()

        try:
            await asyncio.to_thread(
                self._create, name, service, namespace, spec_location,
                workspace_path, environment, profile, env or {},
            )
            await asyncio.wait_for(
                self._await_endpoints(service, namespace),
                timeout=settings.code_location_pod_ready_timeout,
            )
        except Exception:
            await self.stop(name, service)
            raise

        host = service_fqdn(environment, namespace)
        logger.info(
            "code_location_pod_started",
            location=spec_location,
            environment=environment,
            pod=name,
            service=service,
            host=host,
        )
        return name, host, CODELOC_PORT

    async def stop(self, name: str, service: str) -> None:
        """Delete the Pod and its Service. Best-effort; see warm sandbox."""
        namespace = code_location_namespace()
        for kind, delete in (
            ("pod", self._core.delete_namespaced_pod),
            ("service", self._core.delete_namespaced_service),
        ):
            target = name if kind == "pod" else service
            try:
                await asyncio.to_thread(delete, target, namespace)
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                logger.warning(
                    "code_location_pod_stop_failed",
                    kind=kind,
                    name=target,
                    error=str(exc),
                    hint="the object outlives its code location; delete it manually "
                         "if it persists",
                )
        logger.info("code_location_pod_stopped", pod=name, service=service)

    async def is_ready(self, service: str) -> bool:
        """Whether the Service has at least one routable endpoint.

        The Endpoints object is the registry: a location is "alive" exactly
        when Kubernetes says something is listening behind its Service. There
        is deliberately no liveness probe on the Pod — a gRPC server that is up
        but still importing user code would answer a probe and then fail the
        orchestrator's first real query.
        """
        namespace = code_location_namespace()
        try:
            endpoints = await asyncio.to_thread(
                self._core.read_namespaced_endpoints, service, namespace
            )
        except Exception:  # noqa: BLE001 - "cannot read" means "not ready"
            return False
        return bool(self._ready_addresses(endpoints))

    # ── internals ─────────────────────────────────────────────────────

    def _create(
        self, name: str, service: str, namespace: str, location_name: str,
        workspace_path: str, environment: str, profile, env: dict[str, str],
    ) -> None:
        """Create the Service and the Pod (blocking; call in a thread)."""
        volumes, mounts = self._tasks._volumes_for_path(workspace_path, name)
        scheduling = self._tasks._scheduling(profile)

        dns: dict = {}
        dns_policy = str(profile.settings.get("dns_policy") or "")
        if dns_policy:
            dns["dnsPolicy"] = dns_policy
            dns_config = self._tasks._dns_config()
            if dns_policy == "None" and dns_config is not None:
                dns["dnsConfig"] = dns_config
            elif dns_policy == "None":
                dns["dnsPolicy"] = "ClusterFirst"

        pod = build_pod_manifest(
            name=name,
            namespace=namespace,
            location_name=location_name,
            environment=environment,
            image=settings.code_location_container_image,
            image_pull_policy=settings.task_runner_image_pull_policy,
            service_account=settings.task_service_account,
            volumes=[v.to_dict() if hasattr(v, "to_dict") else v for v in volumes],
            volume_mounts=[m.to_dict() if hasattr(m, "to_dict") else m for m in mounts],
            scheduling=scheduling,
            security_context=self._tasks._container_security_context(profile).to_dict(),
            resources=self._tasks._resources(profile).to_dict(),
            pod_security_context=self._tasks._pod_security_context(profile).to_dict(),
            env=env,
            dns=dns,
        )

        # The Service first: creating the Pod before its Service would leave a
        # window in which the location is running but undiscoverable.
        self._core.create_namespaced_service(
            namespace, build_service_manifest(
                name=service, namespace=namespace, environment=environment
            )
        )
        self._core.create_namespaced_pod(namespace, pod)

    async def _await_endpoints(self, service: str, namespace: str) -> None:
        """Poll until the Service has a routable endpoint or the Pod fails."""
        while True:
            try:
                endpoints = await asyncio.to_thread(
                    self._core.read_namespaced_endpoints, service, namespace
                )
            except Exception:  # noqa: BLE001 - not created yet
                endpoints = None
            if endpoints is not None and self._ready_addresses(endpoints):
                return
            await asyncio.sleep(0.5)

    @staticmethod
    def _ready_addresses(endpoints: Any) -> list[str]:
        """Flat list of ready endpoint addresses, tolerating either API shape."""
        addresses: list[str] = []
        for subset in getattr(endpoints, "subsets", None) or []:
            for address in getattr(subset, "addresses", None) or []:
                ip = getattr(address, "ip", None) or (
                    address.get("ip") if isinstance(address, dict) else None
                )
                if ip:
                    addresses.append(ip)
        return addresses


__all__ = [
    "APP_CODE_LOCATION",
    "ASSET_DAG_SUBDIR",
    "CODELOC_PORT",
    "CodeLocationPodBackend",
    "code_location_namespace",
    "CodeLocationPodError",
    "WORKSPACE_MOUNT_PATH",
    "build_pod_manifest",
    "build_service_manifest",
    "pod_name",
    "service_fqdn",
    "service_name",
]
