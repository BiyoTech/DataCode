"""Cross-file deployment contracts (deploy/).

The manifests in ``deploy/`` are correct only in RELATION to each other: a
service's ``depends_on`` against the migrate step, a compose variable against
the Secret that must hold it, a ``CREATE DATABASE`` against the connection
string that names it. Every one of those is a statement in one file about
another file, and nothing but review has been checking them — which is how a
runtime replica ended up as the single service not waiting for migrations, and
how the gitea profile shipped pointing at a database nobody created.

These tests are pure YAML/SQL parsing: no daemon, no cluster, no kustomize
binary, so they run wherever the runtime tests run. They assert CONTRACTS, not
formatting — each one names the failure it prevents, because a contract test
whose reason is lost gets deleted the first time it is inconvenient.

Scope note: ``test_deploy_manifests.py`` pins the SHAPE of the data plane
(StatefulSet, per-replica RWO, lease flags). This file pins the relationships
BETWEEN deployment files. They deliberately do not overlap.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"
_K8S_BASE = _DEPLOY / "k8s" / "base"

# The stacks that RUN the platform. data-plane is excluded on purpose: it is a
# lease-verification harness (its own header says so), it has no migrate
# service and it is not a deployment target.
_APPLICATION_STACKS = ("docker-compose.stack.yml", "docker-compose.cell.yml")

# Compose services that are infrastructure or one-shots, not platform
# services: they must not be required to depend on the migrate step (the
# migrate step depends on THEM, and a database cannot wait for its own
# migration).
_NON_APPLICATION_SERVICES = {
    "postgres",
    "redis",
    "gitea",
    "cell-pg",
    "cell-redis",
    "global-pg",
    "migrate",
    # The edge and the SPA hold no schema; they wait for the services that do.
    "gateway",
    "frontend",
}


def _compose(name: str) -> dict:
    return yaml.safe_load((_DEPLOY / name).read_text())


def _compose_files() -> list[Path]:
    return sorted(_DEPLOY.glob("docker-compose.*.yml"))


def _k8s_documents() -> list[dict]:
    """Every object in the Kustomize base (multi-document files included)."""
    documents = []
    for path in sorted(_K8S_BASE.glob("*.yaml")):
        if path.name in ("kustomization.yaml", "secrets.example.yaml"):
            continue
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind"):
                documents.append(document)
    return documents


def _pod_spec(document: dict) -> dict | None:
    """The PodSpec of a Deployment/StatefulSet/Job, or None."""
    if document["kind"] in ("Deployment", "StatefulSet", "Job"):
        return document["spec"]["template"]["spec"]
    return None


@lru_cache(maxsize=None)
def _render(overlay: str) -> tuple[dict, ...]:
    """The objects an overlay actually applies.

    Parsing the base files answers "what did we write"; only rendering
    answers "what reaches the cluster". The two differ by everything
    kustomize does — namespace assignment on generated objects, the
    overlays' deletions, the ConfigMap name hash — and that gap is precisely
    where the interesting mistakes live.

    --load-restrictor LoadRestrictionsNone is required because the conf
    ConfigMap is generated from the repository's conf/ directory, which sits
    outside deploy/k8s/base/ by design (one source of truth for every shape).

    Cached, and returning an immutable tuple so the cache cannot be poisoned
    by a caller: rendering shells out to kustomize and the overlays are fixed
    for the duration of a test run.
    """
    result = subprocess.run(
        [
            "kubectl",
            "kustomize",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(_DEPLOY / "k8s" / "overlays" / overlay),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(d for d in yaml.safe_load_all(result.stdout) if d)


class TestMigrationOrdering:
    """Every schema-carrying service waits for the one-shot migrate step.

    deploy/README.md states this as THE ordering contract, and the replicas
    run with AUTO_MIGRATE=0 because of it. One service without the dependency
    is not a partial guarantee — it is the one replica that races the
    migration, and the race is won silently.
    """

    @pytest.mark.parametrize("stack", _APPLICATION_STACKS)
    def test_every_application_service_depends_on_migrate(self, stack):
        document = _compose(stack)
        services = document["services"]
        assert "migrate" in services, f"{stack} has no migrate service"

        missing = []
        for name, service in services.items():
            if name in _NON_APPLICATION_SERVICES:
                continue
            depends = service.get("depends_on") or {}
            entry = depends.get("migrate") if isinstance(depends, dict) else None
            if not entry or entry.get("condition") != "service_completed_successfully":
                missing.append(name)

        assert not missing, (
            f"{stack}: {sorted(missing)} start without waiting for the migrate "
            f"step. They race table creation against it, which is exactly what "
            f"the one-shot migrate service exists to prevent."
        )

    @pytest.mark.parametrize("stack", _APPLICATION_STACKS)
    def test_replicas_never_migrate_themselves(self, stack):
        """AUTO_MIGRATE=1 next to a migrate step is two migrators, not one."""
        offenders = []
        for name, service in _compose(stack)["services"].items():
            if name == "migrate":
                continue
            env = service.get("environment") or {}
            if not isinstance(env, dict):
                continue
            if str(env.get("AUTO_MIGRATE", "0")) == "1":
                offenders.append(name)
        assert not offenders, (
            f"{stack}: {sorted(offenders)} set AUTO_MIGRATE=1 while a migrate "
            f"service exists"
        )


class TestConfigMechanism:
    """One config mechanism everywhere: mounted conf/ + a profile.

    A service that mounts a private config file, or that mounts conf/ without
    selecting a profile, resolves the address-free base and falls back to
    code defaults — sqlite, localhost Redis — while looking configured.
    """

    @pytest.mark.parametrize("stack", _APPLICATION_STACKS)
    def test_every_application_service_mounts_conf_with_a_profile(self, stack):
        problems = []
        for name, service in _compose(stack)["services"].items():
            if name in _NON_APPLICATION_SERVICES - {"migrate", "gateway", "frontend"}:
                continue
            if "image" in service and "build" not in service:
                continue  # third-party image, not a platform service
            env = service.get("environment") or {}
            volumes = service.get("volumes") or []
            if not any(str(v).startswith("../conf:/app/conf") for v in volumes):
                problems.append(f"{name}: does not mount ../conf:/app/conf")
            if env.get("CONFIG_PATH") != "/app/conf":
                problems.append(f"{name}: CONFIG_PATH is not /app/conf")
            if not env.get("DATACODE_ENV"):
                problems.append(f"{name}: no DATACODE_ENV (no profile selected)")
        assert not problems, "\n".join(f"{stack}: {p}" for p in problems)

    def test_container_stacks_never_dial_localhost(self):
        """`localhost` inside a container is the container itself.

        A host address that works in dev and resolves to nothing in a
        container is the classic silent-failure config bug; the container
        profiles use compose service names for exactly this reason.

        Only ENVIRONMENT values are examined, and two classes are excluded
        because for them localhost is correct rather than a mistake:
          * healthchecks — a probe dials its OWN container, which is the
            whole point of a container-local probe;
          * browser-facing URLs — the value is handed to a user's browser on
            the host, so it must be the published host address.
        """
        browser_facing = {"GITEA__server__ROOT_URL"}
        pattern = re.compile(r"//(localhost|127\.0\.0\.1):\d+")
        problems = []
        for path in _compose_files():
            document = yaml.safe_load(path.read_text())
            for name, service in (document.get("services") or {}).items():
                environment = service.get("environment") or {}
                # Compose accepts both a mapping and a KEY=VALUE list.
                if isinstance(environment, list):
                    pairs = [
                        entry.split("=", 1)
                        for entry in environment
                        if "=" in entry
                    ]
                else:
                    pairs = list(environment.items())
                for key, value in pairs:
                    if key in browser_facing:
                        continue
                    if pattern.search(str(value)):
                        problems.append(f"{path.name}:{name}:{key}={value}")
        assert not problems, (
            "container stacks dial host addresses:\n" + "\n".join(problems)
        )


class TestSecretsHaveNoPublicDefaults:
    """A shared secret must not have a well-known literal default.

    `${TOKEN:-dev-internal-token}` is convenient locally and an
    authentication bypass anywhere else: a deployment that forgot its
    env-file starts successfully with a value published in this repository.
    Requiring the variable turns that into a startup error naming the file.
    """

    _SECRET_VARIABLES = (
        "DATACODE_INTERNAL_TOKEN",
        "DATACODE_TASK_TOKEN_SECRET",
        "GW_JWT_SECRET",
    )

    def test_no_compose_file_defaults_a_secret(self):
        pattern = re.compile(
            r"\$\{(" + "|".join(self._SECRET_VARIABLES) + r"):-([^}]*)\}"
        )
        problems = []
        for path in _compose_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                match = pattern.search(line)
                if match:
                    problems.append(
                        f"{path.name}:{number}: {match.group(1)} defaults to "
                        f"{match.group(2)!r}"
                    )
        assert not problems, (
            "secrets with inline defaults (use ${VAR:?...} instead):\n"
            + "\n".join(problems)
        )


class TestDatabasesExistBeforeTheyAreDialled:
    """Every database a stack connects to is created by something.

    Postgres creates exactly one database from POSTGRES_DB; everything else
    has to come from init-db/init.sql, and a service whose connection string
    names a database nobody created crash-loops with a message about the
    database, never about the missing CREATE.
    """

    def _databases_created_by_init_sql(self) -> set[str]:
        text = (_DEPLOY / "init-db" / "init.sql").read_text()
        return set(re.findall(r"CREATE DATABASE\s+(\w+)", text, flags=re.IGNORECASE))

    def test_infra_stack_creates_every_database_its_services_name(self):
        compose = _compose("docker-compose.infra.yml")
        created = self._databases_created_by_init_sql()
        created.add(compose["services"]["postgres"]["environment"]["POSTGRES_DB"])

        # gitea declares its database as a GITEA__database__NAME entry in a
        # list-style environment block.
        gitea_env = compose["services"]["gitea"]["environment"]
        names = {
            entry.split("=", 1)[1]
            for entry in gitea_env
            if entry.startswith("GITEA__database__NAME=")
        }
        missing = names - created
        assert not missing, (
            f"gitea connects to {sorted(missing)}, which deploy/init-db/init.sql "
            f"does not create (postgres only creates POSTGRES_DB)"
        )

    def test_the_dagster_plane_only_stacks_where_its_database_exists(self):
        """Dagster's database comes from init.sql, which cell-pg never runs.

        The Dagster compose file used to advertise itself as stackable on any
        stack; a cell's postgres is created with POSTGRES_DB alone, so that
        combination fails at connect time.
        """
        dagster = _compose("docker-compose.dagster.yml")
        database = dagster["x-dagster"]["environment"]["DAGSTER_PG_DB"]
        assert database in self._databases_created_by_init_sql()

        header = (_DEPLOY / "docker-compose.dagster.yml").read_text()[:2000]
        assert "NOT on a cell" in header, (
            "the header must state where this plane may be stacked; the "
            "database it needs only exists on the infra/stack postgres"
        )

    def test_the_dagster_plane_stands_the_runtime_replicas_down(self):
        """Merging an orchestration plane must disable the per-replica one.

        conf/workspace-runtime.yaml defaults to Dagster mode `local` with a
        webserver and daemon per replica, which is right when no plane
        exists. Stacking this file introduces one: without the override each
        replica keeps its own daemon and every schedule fires N+1 times.
        """
        dagster = _compose("docker-compose.dagster.yml")
        stack = _compose("docker-compose.stack.yml")
        runtimes = [
            name for name in stack["services"] if name.startswith("workspace-runtime")
        ]
        for name in runtimes:
            env = (dagster["services"].get(name) or {}).get("environment") or {}
            assert env.get("WORKSPACE_RUNTIME_DAGSTER_MODE") == "remote", (
                f"{name} keeps its local DagsterInstance when the orchestration "
                f"plane is merged"
            )
            assert env.get("WORKSPACE_RUNTIME_DAGSTER_DAEMON_ENABLED") == "false", (
                f"{name} keeps its own daemon: schedules would fire twice"
            )


class TestKubernetesSecretReferences:
    """Every secretKeyRef names a Secret the runbook tells you to create.

    A typo here is a CreateContainerConfigError at rollout time, and the
    event names the Secret but not the file that asked for it. The set is
    small and closed on purpose.
    """

    _DOCUMENTED = {
        "datacode-db",
        "datacode-secrets",
        "datacode-redis",
        "datacode-task-token",
    }

    def _references(self) -> set[str]:
        names = set()
        for document in _k8s_documents():
            spec = _pod_spec(document)
            if not spec:
                continue
            for container in spec.get("containers", []):
                for entry in container.get("env", []) or []:
                    reference = (entry.get("valueFrom") or {}).get("secretKeyRef")
                    if reference:
                        names.add(reference["name"])
        return names

    def test_no_undocumented_secret_is_referenced(self):
        undocumented = self._references() - self._DOCUMENTED
        assert not undocumented, (
            f"manifests reference Secrets the runbook does not create: "
            f"{sorted(undocumented)}"
        )

    def test_every_documented_secret_appears_in_the_example(self):
        text = (_K8S_BASE / "secrets.example.yaml").read_text()
        missing = [name for name in self._references() if name not in text]
        assert not missing, (
            f"secrets.example.yaml does not document: {sorted(missing)}"
        )

    def test_the_internal_token_has_exactly_one_source(self):
        """Sender and receiver must read the SAME Secret key.

        The runtime's /internal/* token used to come from its own Secret
        while gateway/control/agent sent a key from another. Both sides look
        configured; the symptom is a 401 that names neither of them.
        """
        sources = {}
        for document in _k8s_documents():
            spec = _pod_spec(document)
            if not spec:
                continue
            for container in spec.get("containers", []):
                for entry in container.get("env", []) or []:
                    if entry.get("name") != "WORKSPACE_RUNTIME_INTERNAL_TOKEN":
                        continue
                    reference = (entry.get("valueFrom") or {})["secretKeyRef"]
                    sources[document["metadata"]["name"]] = (
                        reference["name"],
                        reference["key"],
                    )
        assert sources, "no workload reads WORKSPACE_RUNTIME_INTERNAL_TOKEN"
        assert len(set(sources.values())) == 1, (
            f"the internal token is read from more than one place: {sources}"
        )


class TestKubernetesExecutionPlanePermissions:
    """The runtime creates Jobs/Pods/Secrets, so it needs an account.

    On the namespace `default` account every sandboxed task fails with a 403
    the user sees as "could not start a sandbox", three layers from the
    cause. The permissions are declared next to the workload for that reason,
    and this test keeps them there.
    """

    def _statefulset(self) -> dict:
        for document in _k8s_documents():
            if (
                document["kind"] == "StatefulSet"
                and document["metadata"]["name"] == "workspace-runtime"
            ):
                return document
        raise AssertionError("workspace-runtime StatefulSet not found")

    def test_the_runtime_runs_as_its_own_service_account(self):
        spec = self._statefulset()["spec"]["template"]["spec"]
        assert spec.get("serviceAccountName") == "workspace-runtime"

    def test_that_account_exists_and_is_bound(self):
        documents = _k8s_documents()
        accounts = {
            d["metadata"]["name"]
            for d in documents
            if d["kind"] == "ServiceAccount"
        }
        assert "workspace-runtime" in accounts

        bindings = [d for d in documents if d["kind"] == "RoleBinding"]
        bound = [
            binding
            for binding in bindings
            if any(
                subject.get("name") == "workspace-runtime"
                and subject.get("namespace") == "datacode"
                for subject in binding["subjects"]
            )
        ]
        assert bound, "the workspace-runtime account has no RoleBinding"
        # In the TASK namespace: the runtime acts there and nowhere else.
        assert {b["metadata"]["namespace"] for b in bound} == {"datacode-tasks"}

    def test_the_role_grants_what_the_backend_actually_calls(self):
        """Including pods/log — the result channel, not a debugging aid.

        P6a chose the Pod log over an HTTP callback, so a Role without
        `pods/log` completes every task and loses every result.
        """
        roles = {
            d["metadata"]["name"]: d
            for d in _k8s_documents()
            if d["kind"] == "Role"
        }
        role = roles.get("task-vehicle-controller")
        assert role, "the task-vehicle Role is missing"

        granted = {
            (group, resource, verb)
            for rule in role["rules"]
            for group in rule["apiGroups"]
            for resource in rule["resources"]
            for verb in rule["verbs"]
        }
        required = {
            ("batch", "jobs", "create"),
            ("batch", "jobs", "delete"),
            ("", "pods", "create"),
            ("", "pods", "list"),
            ("", "pods/log", "get"),
            ("", "secrets", "create"),
            ("", "secrets", "delete"),
            ("", "configmaps", "create"),
        }
        assert required <= granted, (
            f"the Role is missing: {sorted(required - granted)}"
        )

    def test_the_sandbox_account_stays_unbound(self):
        """Permissions belong to the CREATOR, never to the created.

        Task Pods run as task-runner-noperm. A binding for it would let a
        sandbox create Jobs — i.e. escape its own resource and isolation
        limits by creating a Pod without them.
        """
        for binding in _k8s_documents():
            if binding["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            names = {subject.get("name") for subject in binding["subjects"]}
            assert "task-runner-noperm" not in names, (
                "the sandbox service account must have no RoleBinding"
            )


class TestKubernetesLocalInfrastructure:
    """The local infra file completes what the profiles assume exists.

    conf/platform.k8s-*.yaml name four in-cluster endpoints. The base ships
    none of them (a real install points at managed instances), so a local
    cluster needs this file — and its Service names must match the profiles
    exactly, or every service dials a name that does not resolve.
    """

    @pytest.fixture()
    def services(self) -> set[tuple[str, str]]:
        path = _DEPLOY / "k8s" / "local" / "infra.yaml"
        return {
            (d["metadata"]["namespace"], d["metadata"]["name"])
            for d in yaml.safe_load_all(path.read_text())
            if d and d["kind"] == "Service"
        }

    def test_it_provides_every_endpoint_the_profiles_name(self, services):
        assert ("datacode", "postgres") in services
        assert ("datacode", "redis") in services
        assert ("datacode-global", "global-pg") in services
        assert ("datacode-global", "redis") in services

    def test_the_profiles_still_name_those_services(self):
        """Renaming a Service in one place only is the failure this prevents."""
        cell = (_REPO_ROOT / "conf" / "platform.k8s-cell.yaml").read_text()
        global_ = (_REPO_ROOT / "conf" / "platform.k8s-global.yaml").read_text()
        assert "postgres.datacode.svc.cluster.local" in cell
        assert "redis.datacode.svc.cluster.local" in cell
        assert "global-pg.datacode-global.svc.cluster.local" in cell
        assert "redis.datacode-global.svc.cluster.local" in global_

    def test_the_cell_redis_carries_the_label_the_policy_selects(self):
        """egress-proxy-policy allows `app: redis` in the datacode namespace.

        A differently-labelled Redis is denied by the NetworkPolicy, and the
        proxy reports it as "grant store unreachable" — i.e. every sandbox
        loses egress for a reason that names neither Redis nor the policy.
        """
        path = _DEPLOY / "k8s" / "local" / "infra.yaml"
        for document in yaml.safe_load_all(path.read_text()):
            if not document or document["kind"] != "Deployment":
                continue
            if document["metadata"]["namespace"] != "datacode":
                continue
            if document["metadata"]["name"] != "redis":
                continue
            labels = document["spec"]["template"]["metadata"]["labels"]
            assert labels.get("app") == "redis"
            return
        raise AssertionError("no redis Deployment in the datacode namespace")

    def test_it_is_not_part_of_the_kustomize_base(self):
        """Local-only, and applied explicitly.

        In the base, every cluster — including the first production one —
        would get a database whose storage is an emptyDir.
        """
        text = (_K8S_BASE / "kustomization.yaml").read_text()
        assert "local/" not in text and "infra.yaml" not in text


def _kustomize_available() -> bool:
    return shutil.which("kubectl") is not None


def test_the_rendered_contracts_are_not_silently_skipped():
    """In CI, a skipped check is a hole in the record — never a pass.

    The class below is skipped when kubectl is absent, which is right on a
    laptop and wrong in CI: every assertion about what the cluster actually
    receives would vanish with no failure. GitHub-hosted runners ship
    kubectl, so this asserts that environment fact rather than trusting it —
    if a future runner image drops it, this fails instead of the coverage
    disappearing.
    """
    if not os.getenv("CI"):
        pytest.skip("local run; the class below skips on its own")
    assert _kustomize_available(), (
        "kubectl is missing in CI, so every rendered-overlay contract would "
        "skip silently. Install it in the workflow rather than accepting the "
        "skip."
    )


@pytest.mark.skipif(
    not _kustomize_available(), reason="kubectl (kustomize) not installed"
)
class TestRenderedOverlays:
    """What the cluster receives, not what the base files say.

    Every other class here parses source files. That cannot see anything
    kustomize DOES: which objects an overlay deleted, what namespace a
    GENERATED object landed in, what the hashed ConfigMap is called. The gap
    is not theoretical — the conf ConfigMap was generated without a namespace
    for as long as these manifests existed, so it applied into `default`
    while every pod mounting it ran in `datacode` or `datacode-global`. The
    pods stay in ContainerCreating on a ConfigMap that exists, just not where
    they are, and no amount of reading the base reveals it.
    """

    # Rendered once per overlay for the whole session (see _render's cache):
    # each call shells out to kustomize, and the overlays cannot change
    # mid-run. Plain fixtures delegating to that cache, rather than
    # class-scoped ones, which pytest deprecates in the instance-method form.
    @pytest.fixture()
    def cell(self) -> tuple[dict, ...]:
        return _render("cell")

    @pytest.fixture()
    def global_(self) -> tuple[dict, ...]:
        return _render("global")

    @pytest.fixture()
    def kind(self) -> tuple[dict, ...]:
        return _render("kind")

    # ── Namespace correctness of every reference ────────────────────────

    @staticmethod
    def _assert_mounts_resolve(documents: tuple[dict, ...]) -> None:
        """Every mounted ConfigMap exists IN THE MOUNTING POD'S namespace."""
        available = {
            (d["metadata"].get("namespace"), d["metadata"]["name"])
            for d in documents
            if d["kind"] == "ConfigMap"
        }
        problems = []
        for document in documents:
            spec = _pod_spec(document)
            if not spec:
                continue
            namespace = document["metadata"].get("namespace")
            for volume in spec.get("volumes", []) or []:
                name = (volume.get("configMap") or {}).get("name")
                if name and (namespace, name) not in available:
                    problems.append(
                        f"{namespace}/{document['kind']} "
                        f"{document['metadata']['name']} mounts ConfigMap "
                        f"{name!r}, which this overlay does not create in "
                        f"{namespace}"
                    )
        assert not problems, "\n".join(problems)

    def test_cell_configmap_mounts_resolve(self, cell):
        self._assert_mounts_resolve(cell)

    def test_global_configmap_mounts_resolve(self, global_):
        self._assert_mounts_resolve(global_)

    def test_kind_configmap_mounts_resolve(self, kind):
        self._assert_mounts_resolve(kind)

    @staticmethod
    def _assert_service_accounts_resolve(documents: tuple[dict, ...]) -> None:
        """A named ServiceAccount must exist in the pod's own namespace.

        Kubernetes does not reject the reference; it just never admits the
        pod, and every API call the workload makes would have failed anyway.
        """
        available = {
            (d["metadata"].get("namespace"), d["metadata"]["name"])
            for d in documents
            if d["kind"] == "ServiceAccount"
        }
        problems = []
        for document in documents:
            spec = _pod_spec(document)
            if not spec:
                continue
            account = spec.get("serviceAccountName")
            namespace = document["metadata"].get("namespace")
            if account and (namespace, account) not in available:
                problems.append(
                    f"{namespace}/{document['metadata']['name']} runs as "
                    f"{account!r}, which does not exist in {namespace}"
                )
        assert not problems, "\n".join(problems)

    def test_cell_service_accounts_resolve(self, cell):
        self._assert_service_accounts_resolve(cell)

    def test_global_service_accounts_resolve(self, global_):
        self._assert_service_accounts_resolve(global_)

    @staticmethod
    def _assert_selectors_match_something(documents: tuple[dict, ...]) -> None:
        """A Service or PDB whose selector matches no pod is inert.

        Neither is an error to Kubernetes: the Service simply has no
        endpoints (callers get connection refused, which reads like the
        workload is down) and the PDB protects nothing (a drain evicts every
        replica at once). Renaming a workload's labels without updating both
        is how that happens, and nothing else notices.
        """
        pods = [
            (
                d["metadata"].get("namespace"),
                d["spec"]["template"]["metadata"].get("labels", {}),
            )
            for d in documents
            if d["kind"] in ("Deployment", "StatefulSet")
        ]

        def matches(namespace: str, selector: dict) -> bool:
            return any(
                pod_namespace == namespace
                and all(labels.get(k) == v for k, v in selector.items())
                for pod_namespace, labels in pods
            )

        problems = []
        for document in documents:
            namespace = document["metadata"].get("namespace")
            name = document["metadata"]["name"]
            if document["kind"] == "Service":
                selector = document["spec"].get("selector") or {}
                if selector and not matches(namespace, selector):
                    problems.append(
                        f"Service {namespace}/{name} selects no pod: {selector}"
                    )
            elif document["kind"] == "PodDisruptionBudget":
                selector = (document["spec"].get("selector") or {}).get(
                    "matchLabels", {}
                )
                if selector and not matches(namespace, selector):
                    problems.append(
                        f"PDB {namespace}/{name} protects nothing: {selector}"
                    )
        assert not problems, "\n".join(problems)

    def test_cell_selectors_match_something(self, cell):
        self._assert_selectors_match_something(cell)

    def test_global_selectors_match_something(self, global_):
        self._assert_selectors_match_something(global_)

    def test_ingress_backends_exist_with_the_named_port(self, cell):
        """An Ingress backend is resolved at request time, not at apply time.

        A wrong service name or port applies cleanly and answers 503 to
        every user — the one failure mode that reaches production looking
        like an application outage rather than a manifest error.
        """
        ports = {
            (d["metadata"].get("namespace"), d["metadata"]["name"]): {
                p.get("port") for p in d["spec"]["ports"]
            }
            for d in cell
            if d["kind"] == "Service"
        }
        problems = []
        for document in cell:
            if document["kind"] != "Ingress":
                continue
            namespace = document["metadata"].get("namespace")
            for rule in document["spec"].get("rules", []):
                for path in (rule.get("http") or {}).get("paths", []):
                    backend = path["backend"]["service"]
                    available = ports.get((namespace, backend["name"]))
                    if available is None:
                        problems.append(
                            f"ingress backend {namespace}/{backend['name']} "
                            f"does not exist"
                        )
                    elif backend["port"].get("number") not in available:
                        problems.append(
                            f"ingress backend {namespace}/{backend['name']} "
                            f"has no port {backend['port']}; it exposes "
                            f"{sorted(available)}"
                        )
        assert not problems, "\n".join(problems)

    @staticmethod
    def _assert_namespaces_declared(documents: tuple[dict, ...]) -> None:
        """No namespaced object may rely on the apply context.

        An object without a namespace goes wherever kubectl's current
        context points — `default` for a piped apply — which is both wrong
        and invisible until something fails to find it.
        """
        cluster_scoped = {"Namespace", "ClusterRole", "ClusterRoleBinding"}
        missing = [
            f"{d['kind']}/{d['metadata']['name']}"
            for d in documents
            if d["kind"] not in cluster_scoped
            and not d["metadata"].get("namespace")
        ]
        assert not missing, (
            f"objects with no namespace (they would apply into `default`): "
            f"{sorted(missing)}"
        )

    def test_cell_declares_every_namespace(self, cell):
        self._assert_namespaces_declared(cell)

    def test_global_declares_every_namespace(self, global_):
        self._assert_namespaces_declared(global_)

    def test_kind_declares_every_namespace(self, kind):
        self._assert_namespaces_declared(kind)

    # ── Tier boundaries ─────────────────────────────────────────────────

    def test_the_cell_tier_runs_no_identity(self, cell):
        """Identity in a cell would dial the CELL database for global data.

        The overlay's deletions are the only thing preventing it, and a
        deletion is silently ineffective when the object it names is
        renamed — so the property is asserted on the result, not on the
        patch list.
        """
        offenders = [
            f"{d['kind']}/{d['metadata']['name']}"
            for d in cell
            if "identity" in d["metadata"]["name"]
        ]
        assert not offenders, f"the cell overlay still ships {offenders}"

    def test_the_global_tier_runs_only_identity(self, global_):
        """No user code, no data plane, no edge — and nothing in a cell's
        namespace, which is the boundary that bounds blast radius."""
        workloads = {
            d["metadata"]["name"]
            for d in global_
            if d["kind"] in ("Deployment", "StatefulSet")
        }
        assert workloads == {"identity-service"}, (
            f"the global tier ships more than identity: {sorted(workloads)}"
        )

        namespaces = {
            d["metadata"].get("namespace")
            for d in global_
            if d["metadata"].get("namespace")
        }
        assert namespaces == {"datacode-global"}, (
            f"the global tier reaches into {sorted(namespaces)}"
        )

    def test_the_global_tier_ships_an_exact_object_set(self, global_):
        """The overlay is a DENY-list, so a new base file leaks in silently.

        This is the structural weakness of "base carries everything, the
        global overlay deletes 36 objects": adding a file to the base is a
        one-line change that ships it to both tiers, and nothing about the
        global overlay looks wrong afterwards. That happened to this
        repository when the runtime's RBAC was added — three deletions had
        to be written by hand, and forgetting them would have granted a
        ServiceAccount in a namespace the global tier does not create.

        Pinning the exact set converts that silence into a failure naming
        the new object. When a new object genuinely belongs here, this list
        is the one place that says so — which is the point.
        """
        def identify(document: dict) -> tuple[str, str]:
            name = document["metadata"]["name"]
            # The conf ConfigMap's name carries a content hash, which changes
            # whenever conf/ is edited — match it by its stable prefix.
            if document["kind"] == "ConfigMap" and name.startswith("datacode-conf-"):
                name = "datacode-conf-<hash>"
            return (document["kind"], name)

        shipped = {identify(d) for d in global_}
        expected = {
            ("Namespace", "datacode-global"),
            ("ConfigMap", "datacode-conf-<hash>"),
            ("Service", "identity-service"),
            ("Deployment", "identity-service"),
            ("PodDisruptionBudget", "identity-service"),
            ("Job", "identity-migrate"),
        }
        assert shipped == expected, (
            f"unexpected in the global tier: {sorted(shipped - expected)}; "
            f"missing: {sorted(expected - shipped)}"
        )

    def test_the_global_tier_has_no_sandbox_plane(self, global_):
        kinds = {d["kind"] for d in global_}
        assert "NetworkPolicy" not in kinds
        assert "ResourceQuota" not in kinds

    # ── The kind overlay is the cell shape, scaled down ─────────────────

    def test_kind_scales_down_without_weakening_isolation(self, cell, kind):
        """A verification overlay that dropped the policies would verify
        nothing, so only size may differ."""
        def policies(documents):
            return {
                d["metadata"]["name"]
                for d in documents
                if d["kind"] == "NetworkPolicy"
            }

        assert policies(kind) == policies(cell)

        runtime = next(
            d for d in kind
            if d["kind"] == "StatefulSet"
            and d["metadata"]["name"] == "workspace-runtime"
        )
        assert runtime["spec"]["replicas"] == 1
        claim = runtime["spec"]["volumeClaimTemplates"][0]
        assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi", (
            "the 100Gi production claim can exceed a local node's "
            "allocatable and strand the pod in Pending forever"
        )

    def test_kind_publishes_the_entry_points(self, kind):
        """No ingress controller runs locally, so the two Services a human
        needs must be reachable without one."""
        types = {
            d["metadata"]["name"]: d["spec"].get("type")
            for d in kind
            if d["kind"] == "Service"
        }
        assert types.get("gateway") == "NodePort"
        assert types.get("frontend") == "NodePort"

    # ── Structural validity ─────────────────────────────────────────────

    @pytest.mark.parametrize("overlay", ["cell", "global", "kind"])
    def test_every_object_is_structurally_complete(self, overlay):
        """apiVersion / kind / metadata.name on every rendered object.

        Deliberately NOT `kubectl apply --dry-run=client`: that was tried
        here and it validates almost nothing without a cluster — it accepts
        `replicas: "two"` and an invented `imagePullPolicy`, even with
        --validate=strict, because the schema lives on the API server. A
        test whose docstring promised schema validation while providing
        none is worse than no test: it makes the next reader stop looking.

        Real schema validation needs `--dry-run=server`, i.e. a cluster, so
        it belongs in the deploy runbook rather than in CI. What IS checkable
        here is that every document kustomize emits is a complete, named,
        typed object — which catches a malformed patch producing a fragment.
        """
        problems = []
        for document in _render(overlay):
            name = (document.get("metadata") or {}).get("name")
            if not document.get("apiVersion"):
                problems.append(f"{document.get('kind')}/{name}: no apiVersion")
            if not document.get("kind"):
                problems.append(f"{name}: no kind")
            if not name:
                problems.append(f"{document.get('kind')}: no metadata.name")
        assert not problems, "\n".join(problems)

    def test_the_local_infrastructure_parses_as_kubernetes_objects(self):
        """Applied by hand, so nothing else would ever look at it."""
        documents = [
            d
            for d in yaml.safe_load_all(
                (_DEPLOY / "k8s" / "local" / "infra.yaml").read_text()
            )
            if d
        ]
        assert documents, "the local infra file renders nothing"
        for document in documents:
            assert document.get("apiVersion")
            assert document.get("kind")
            assert document["metadata"]["name"]
            assert document["metadata"]["namespace"], (
                f"{document['kind']}/{document['metadata']['name']} has no "
                f"namespace; applied by hand, it would land in `default`"
            )

    # ── Image tags ──────────────────────────────────────────────────────

    def test_every_platform_image_carries_the_same_tag(self, cell):
        """`make k8s-tag` rewrites them together; a half-rewritten set is a
        cluster running two versions of one release."""
        tags = set()
        for document in cell:
            spec = _pod_spec(document)
            if not spec:
                continue
            for container in spec.get("containers", []):
                image = container["image"]
                if image.startswith("datacode/"):
                    tags.add(image.rsplit(":", 1)[1])
        assert len(tags) == 1, f"platform images carry mixed tags: {sorted(tags)}"
