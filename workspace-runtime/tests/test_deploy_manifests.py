"""Deployment-manifest contract tests (design 06 P4a/P4b).

The storage contract in ``datacode_common.materialize`` makes unsafe
combinations impossible to BOOT — but only for values that reach the process.
The manifests are where a well-meaning edit (a shared PVC "to save storage", a
Deployment "because StatefulSets are annoying") can silently reintroduce what
the contract exists to prevent. These tests pin the manifests to the shapes
the design published:

* K8s: StatefulSet + per-replica RWO volumeClaimTemplates + headless service,
  instance_local + leases + in-process env lock (the 4b target);
* compose: the P4a transitional shape — shared bind mount + leases + the
  Redis env lock that combination REQUIRES.

They run wherever the runtime tests run (the manifests are data), so a
manifest regression fails CI rather than a staging boot.

The K8s manifest moved under deploy/k8s/base/ when the plain manifests became
a Kustomize base; these tests parse the base file directly, which keeps them
running wherever the runtime tests run (no kustomize binary required).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K8S_MANIFEST = _REPO_ROOT / "deploy" / "k8s" / "base" / "workspace-runtime.yaml"
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "docker-compose.data-plane.yml"
_RUNTIME_DOCKERFILE = _REPO_ROOT / "workspace-runtime" / "Dockerfile"


def _dockerfile_instructions(path: Path) -> list[tuple[str, str]]:
    """(instruction, arguments) pairs, comments and continuations aside.

    Parsed rather than shelled out to a builder: this runs in CI, where no
    daemon exists and a build would take minutes to tell us about a missing
    COPY.
    """
    instructions = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        instruction, _, arguments = line.partition(" ")
        instructions.append((instruction.upper(), arguments.strip()))
    return instructions


def _k8s_docs() -> dict:
    docs = {}
    with open(_K8S_MANIFEST) as handle:
        for doc in yaml.safe_load_all(handle):
            docs[doc["kind"]] = doc
    return docs


def _container_env(statefulset: dict) -> dict[str, str]:
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    env: dict[str, str] = {}
    for entry in container["env"]:
        if "value" in entry:
            env[entry["name"]] = str(entry["value"])
    return env


class TestK8sTargetShape:
    """The 4b target: replica-private storage, leases on, locks in-process."""

    @pytest.fixture()
    def docs(self) -> dict:
        return _k8s_docs()

    def test_statefulset_not_deployment(self, docs):
        # A Deployment gives pods random names and no per-replica volume
        # identity; both are load-bearing for lease takeover and RWO mounts.
        assert "StatefulSet" in docs
        assert "Deployment" not in docs

    def test_per_replica_rwo_volume_claim(self, docs):
        sts = docs["StatefulSet"]
        claims = sts["spec"]["volumeClaimTemplates"]
        assert len(claims) == 1, "exactly one per-replica claim expected"
        assert claims[0]["spec"]["accessModes"] == ["ReadWriteOnce"]
        # The mount must be the materialize root the env declares.
        mounts = sts["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ]
        root = _container_env(sts)["WORKSPACE_RUNTIME_MATERIALIZE_ROOT"]
        assert any(m["name"] == claims[0]["metadata"]["name"] and
                   m["mountPath"] == root for m in mounts)

    def test_headless_service(self, docs):
        service = docs["Service"]
        assert service["spec"]["clusterIP"] == "None", (
            "a ClusterIP would load balance working-copy traffic, which the "
            "lease exists to prevent"
        )

    def test_env_lock_override_is_the_variable_the_code_reads(self, docs):
        """An override nobody reads is worse than no override.

        This service used to read a RETIRED service prefix here, so the
        manifest below set a variable the code never looked at: the deployment
        appeared to select a backend while the process silently kept the
        configured default. Asserting the manifest key against the name the
        lock actually consults is what makes that unrepeatable.
        """
        from src.sqlmesh.env_locks import locks

        env = _container_env(docs["StatefulSet"])
        assert locks.backend_env_var == "WORKSPACE_RUNTIME_ENV_LOCK_BACKEND"
        assert locks.backend_env_var in env

    def test_declared_shape_matches_the_storage_contract(self, docs):
        env = _container_env(docs["StatefulSet"])
        assert env["WORKSPACE_RUNTIME_MATERIALIZE_MOUNT_KIND"] == "instance_local"
        assert env["WORKSPACE_RUNTIME_LEASE_ENABLED"] == "true"
        # 4b: private media + leases make the in-process env lock sufficient.
        assert env["WORKSPACE_RUNTIME_ENV_LOCK_BACKEND"] == "in_memory"

        # And that combination must actually validate — the manifest and the
        # contract must never drift apart.
        from datacode_common.materialize import validate_storage_contract

        contract = validate_storage_contract(
            root=env["WORKSPACE_RUNTIME_MATERIALIZE_ROOT"],
            mount_kind=env["WORKSPACE_RUNTIME_MATERIALIZE_MOUNT_KIND"],
            lease_enabled=env["WORKSPACE_RUNTIME_LEASE_ENABLED"] == "true",
            distributed_env_lock=(
                env["WORKSPACE_RUNTIME_ENV_LOCK_BACKEND"] in ("auto", "redis")
            ),
        )
        assert contract.warnings == ()

    def test_stable_runtime_identity_from_pod_name(self, docs):
        sts = docs["StatefulSet"]
        env = _container_env(sts)
        assert "DATACODE_RUNTIME_ID" not in env, (
            "DATACODE_RUNTIME_ID must come from fieldRef (pod name), not a "
            "static value — a static id makes every replica claim the same "
            "leases"
        )
        raw = [
            e for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] == "DATACODE_RUNTIME_ID"
        ]
        assert raw and raw[0]["valueFrom"]["fieldRef"]["fieldPath"] == \
            "metadata.name"

    def test_no_dead_env_vars(self, docs):
        """Variables the process never reads must not appear in the manifest.

        REDIS_HOST / REDIS_PORT / WORKSPACE_RUNTIME_DB_* were shipped here for
        a config they do not reach: the runtime reads Redis and database
        settings from the platform config (only DB_PASSWORD is env-read), so
        those entries described a configuration that never took effect — and
        made the manifest look like the source of truth for addresses it does
        not hold. Addresses now arrive via the conf ConfigMap + DATACODE_ENV.
        """
        env = _container_env(docs["StatefulSet"])
        dead = {
            "REDIS_HOST", "REDIS_PORT", "REDIS_DB",
            "WORKSPACE_RUNTIME_DB_TYPE", "WORKSPACE_RUNTIME_DB_HOST",
            "WORKSPACE_RUNTIME_DB_PORT", "WORKSPACE_RUNTIME_DB_NAME",
            "WORKSPACE_RUNTIME_DB_USERNAME", "WORKSPACE_RUNTIME_DB_PASSWORD",
        }
        present = dead & set(env)
        assert not present, f"manifest declares env vars no code reads: {sorted(present)}"

    def test_config_arrives_via_the_conf_mount(self, docs):
        """The K8s path uses the same config mechanism as everything else.

        CONFIG_PATH + a mounted conf/ (the datacode-conf ConfigMap). Without
        this, the pod resolves platform config to pydantic defaults — sqlite,
        localhost Redis — while LEASE_ENABLED=true, and boots broken.
        """
        sts = docs["StatefulSet"]
        env = _container_env(sts)
        assert env.get("CONFIG_PATH") == "/app/conf"
        assert env.get("DATACODE_ENV"), (
            "no DATACODE_ENV: the pod would resolve the address-free base "
            "and fall back to arbitrary code defaults"
        )
        mounts = sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        conf_mounts = [m for m in mounts if m.get("mountPath") == "/app/conf"]
        assert conf_mounts and conf_mounts[0].get("readOnly") is True
        volumes = sts["spec"]["template"]["spec"]["volumes"]
        assert any(
            v.get("configMap", {}).get("name", "").startswith("datacode-conf")
            for v in volumes
        )


class TestComposeTransitionalShape:
    """The P4a transition: shared volume + leases REQUIRES the Redis lock."""

    @pytest.fixture()
    def compose(self) -> dict:
        with open(_COMPOSE_FILE) as handle:
            return yaml.safe_load(handle)

    def _runtime_env(self, compose: dict, service: str) -> dict[str, str]:
        entry = compose["services"][service]
        env = dict(entry.get("environment") or {})
        for anchor in ("x-workspace-runtime",):
            pass  # merged anchors are already resolved by the loader
        return {str(k): str(v) for k, v in env.items()}

    def test_two_replicas_with_stable_identities(self, compose):
        runtimes = [
            name for name in compose["services"]
            if name.startswith("workspace-runtime")
        ]
        assert len(runtimes) == 2
        for name in runtimes:
            env = compose["services"][name]["environment"]
            assert env["DATACODE_RUNTIME_ID"], (
                f"{name} needs a stable runtime id (lease identity)"
            )

    def test_declared_shape_is_the_p4a_transition(self, compose):
        # Collect the shared anchor environment from a concrete service: the
        # lease settings live in the anchor, the identity in the override.
        env = compose["services"]["workspace-runtime-1"]["environment"]
        assert env["WORKSPACE_RUNTIME_LEASE_ENABLED"] == "true"
        assert env["WORKSPACE_RUNTIME_MATERIALIZE_MOUNT_KIND"] == "shared"

        # shared + leases is only valid WITH the distributed lock.
        from datacode_common.materialize import validate_storage_contract

        backend = env.get(
            "WORKSPACE_RUNTIME_ENV_LOCK_BACKEND", "auto"
        )  # absent = service default (auto)
        contract = validate_storage_contract(
            root=env["WORKSPACE_RUNTIME_MATERIALIZE_ROOT"],
            mount_kind="shared",
            lease_enabled=True,
            distributed_env_lock=backend in ("auto", "redis"),
        )
        assert any("P4a transitional" in w for w in contract.warnings)


class TestRuntimeImageShipsTheLocalTaskVehicle:
    """`task.backend=local` needs the runner package INSIDE this image.

    The local vehicle spawns `python -m src.main` with the package as the
    child's cwd; in a container there is no sibling checkout to fall back to,
    so an image without it refuses every sandboxed task — which the UI reports
    as "could not start a sandbox to install the dependencies", three layers
    away from the omission. The Kubernetes backend is unaffected: it runs the
    separate datacode/task-runner image.
    """

    @pytest.fixture()
    def instructions(self) -> list[tuple[str, str]]:
        return _dockerfile_instructions(_RUNTIME_DOCKERFILE)

    def _copies(self, instructions) -> dict[str, str]:
        """destination -> source, for every COPY in the image."""
        copies = {}
        for instruction, arguments in instructions:
            if instruction != "COPY":
                continue
            parts = arguments.split()
            if len(parts) >= 2:
                copies[parts[1].rstrip("/")] = parts[0].rstrip("/")
        return copies

    def test_the_package_is_baked_in(self, instructions):
        copies = self._copies(instructions)
        assert any(dest.startswith("task-runner/") for dest in copies), (
            "the runtime image does not ship task-runner/, so task.backend="
            "local cannot start a vehicle (deps install, agent shell)"
        )

    def test_it_lands_where_the_backend_resolves_it_by_default(self, instructions):
        """The empty `task.runner_root` resolves to <image root>/task-runner.

        That path is parents[4] of the backend module, i.e. the WORKDIR the
        service sources are copied under. Pinning both halves here is what
        makes the default hold: a COPY into another directory would satisfy
        the test above and still not be found.
        """
        workdirs = [
            arguments
            for instruction, arguments in instructions
            if instruction == "WORKDIR"
        ]
        image_root = workdirs[0]
        copies = self._copies(instructions)
        assert copies.get("workspace-runtime/src") == "workspace-runtime/src", (
            "the service package must sit directly under the image root for "
            "the default runner resolution to reach task-runner/"
        )
        runner_dest = next(
            dest for dest in copies if dest.startswith("task-runner/")
        )
        expected = f"{image_root}/{runner_dest.split('/')[0]}"
        assert expected == "/app/task-runner"


def _go_modules() -> list[Path]:
    """Every Go module in the repo (venvs and node_modules excluded)."""
    return [
        path for path in _REPO_ROOT.glob("*/go.mod")
        if "node_modules" not in path.parts and ".venv" not in path.parts
    ]


def _local_replaces(go_mod: Path) -> list[str]:
    """Repo-relative paths a go.mod replaces with a local directory.

    Only filesystem replaces are interesting: a module replaced with a local
    path is resolved from the build context, so the Dockerfile must COPY it.
    Version replaces (``=> example.com/x v1.2.3``) download normally.
    """
    targets = []
    module_dir = go_mod.parent
    for line in go_mod.read_text().splitlines():
        line = line.strip()
        if not line.startswith("replace "):
            continue
        # Skip the block form's opening line ("replace (") and version
        # replaces ("=> example.com/x v1.2.3"), which download normally.
        _, arrow, target = line.partition("=>")
        if not arrow:
            continue
        target = target.strip().split()[0] if target.strip() else ""
        if not (target.startswith("../") or target.startswith("./")
                or target == "." or target == ".." or target.startswith("/")):
            continue
        resolved = (module_dir / target).resolve()
        targets.append(resolved.relative_to(_REPO_ROOT.resolve()).as_posix())
    return targets


class TestGoDockerfileBuildContext:
    """A local `replace` target must be inside the Docker build context.

    gateway and identity-service both replace ``github.com/datacode/shared``
    with ``../shared/go``, so their Dockerfiles are built from the repository
    root and must COPY that directory before ``go mod download``. Miss the
    COPY and the build dies with "replacement directory does not exist" — at
    image-build time, i.e. in the slowest possible feedback loop.
    """

    def test_every_go_module_with_a_local_replace_has_a_dockerfile(self):
        missing = [
            str(go_mod.parent.name) for go_mod in _go_modules()
            if _local_replaces(go_mod) and not (go_mod.parent / "Dockerfile").exists()
        ]
        assert not missing, f"Go modules without a Dockerfile: {sorted(missing)}"

    def test_dockerfile_copies_every_local_replace_target(self):
        problems = []
        for go_mod in _go_modules():
            dockerfile = go_mod.parent / "Dockerfile"
            if not dockerfile.exists():
                continue
            text = dockerfile.read_text()
            copied = {
                line.split(None, 1)[1].split()[0].rstrip("/")
                for line in text.splitlines()
                if line.strip().startswith("COPY ")
            }
            for target in _local_replaces(go_mod):
                if not any(c.startswith(target.rstrip("/")) for c in copied):
                    problems.append(
                        f"{dockerfile.relative_to(_REPO_ROOT).as_posix()} does "
                        f"not COPY {target}/ (required by go.mod replace)"
                    )
        assert not problems, "\n".join(sorted(problems))

    def test_build_context_is_the_repo_root(self):
        """Every compose build of a Go service passes the repo root as context.

        A `context: gateway/` build can never satisfy the replace — the path
        escapes the context root — no matter what the Dockerfile says.
        """
        problems = []
        for compose in sorted((_REPO_ROOT / "deploy").glob("docker-compose.*.yml")):
            document = yaml.safe_load(compose.read_text())
            for name, service in (document.get("services") or {}).items():
                build = service.get("build")
                if not isinstance(build, dict):
                    continue
                dockerfile = build.get("dockerfile", "")
                if not dockerfile.endswith("/Dockerfile"):
                    continue
                module_dir = _REPO_ROOT / Path(dockerfile).parent
                if not (module_dir / "go.mod").exists():
                    continue
                if not _local_replaces(module_dir / "go.mod"):
                    continue
                if build.get("context") != "..":
                    problems.append(
                        f"{compose.name}:{name} builds {dockerfile} with "
                        f"context {build.get('context')!r}; a local replace "
                        f"needs the repo root ('..')"
                    )
        assert not problems, "\n".join(problems)
