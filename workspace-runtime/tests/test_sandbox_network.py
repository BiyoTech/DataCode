"""P6b tests: per-task credentials, derived egress, and grant publication.

The assertions here are the Q5 constraints and the fail-closed directions. A
regression in any of them would be a security regression, not a functional one:

* a writable warehouse account must never reach a profile that runs user code;
* a sandbox with no publishable egress grant must not start;
* the destinations must come from the working copy, so they cannot drift wider.
"""

from __future__ import annotations

import time
from typing import Optional

import pytest
from datacode_common.egress import EgressDestination
from datacode_common.egress_grant import (
    EgressGrant,
    EgressGrantStore,
    EgressGrantUnavailable,
)
from datacode_common.isolation import IsolationLevel, IsolationProfile, build_isolation_policy
from datacode_common.security.task_token import (
    TASK_TOKEN_ENV,
    TASK_TOKEN_SECRET_ENV,
    verify_task_token,
)
from datacode_common.task_protocol import TaskResult, TaskSpec, TaskStatus

from src.tasks.backends.base import TaskBackend
from src.tasks.credentials import (
    ENV_EGRESS_PROXY,
    ENV_HTTPS_PROXY,
    WAREHOUSE_READONLY,
    WAREHOUSE_READWRITE,
    CredentialPolicyError,
    assert_write_grade_allowed,
    build_sandbox_credentials,
    warehouse_grade_of,
)
from src.tasks.egress import git_destination, warehouse_destinations_of

SECRET = "runtime-test-signing-secret"
PROXY = "http://egress-proxy.datacode.svc:3128"


# ── helpers ───────────────────────────────────────────────────────────────


def _profile(name: str, **settings) -> IsolationProfile:
    """Build a profile through the real loader, so field names stay honest."""
    document = {
        "profiles": {
            name: {
                "isolation": settings.pop("isolation", "container"),
                "runtime_class": "runc",
                **settings,
            }
        },
        "default_profile": name,
    }
    # The loader requires a fail-safe member; add a disabled one when needed.
    if name not in ("L3_strong", "L3_base", "L3_proc"):
        document["profiles"]["L3_base"] = {
            "isolation": "container",
            "runtime_class": "runc",
            "allowed_task_kinds": [],
        }
        document["default_profile"] = "strongest_enabled"
    return build_isolation_policy(document).profile(name)


def _spec(**overrides) -> TaskSpec:
    base = {
        "task_id": "task0001",
        "kind": "sqlmesh.apply",
        "workspace_path": "/data/workspaces/t1/ws1/dev",
        "environment": "dev",
        "tenant_id": "tenant-1",
        "workspace_id": "ws-1",
        "isolation_profile": "L2_container",
        "actor_user_id": "user-9",
        "timeout_seconds": 600,
    }
    base.update(overrides)
    return TaskSpec(**base)


class _RecordingBackend(TaskBackend):
    def __init__(self) -> None:
        self.credentials: list[object] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def supported_levels(self) -> frozenset[IsolationLevel]:
        return frozenset({IsolationLevel.CONTAINER})

    async def execute(self, spec, profile, credentials=None) -> TaskResult:
        self.credentials.append(credentials)
        return TaskResult(spec.task_id, TaskStatus.SUCCEEDED)


class _NullAuditor:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def record_submitted(self, spec, *, backend) -> None:
        self.events.append("submitted")

    async def record_finished(self, spec, result) -> None:
        self.events.append("finished")


class _MemoryGrants(EgressGrantStore):
    """A grant store with no shared backend and no ambiguity about it."""

    def __init__(self, *, failing: bool = False) -> None:
        async def no_client():
            return None

        super().__init__(allow_local_fallback=True, client_factory=no_client)
        self.failing = failing
        self.published: list[EgressGrant] = []
        self.revoked: list[str] = []

    async def publish(self, grant, *, ttl_seconds: int) -> None:
        if self.failing:
            raise EgressGrantUnavailable("no shared grant store")
        self.published.append(grant)

    async def revoke(self, task_id: str) -> None:
        self.revoked.append(task_id)


# ── the Q5 constraint ─────────────────────────────────────────────────────


class TestWarehouseGradePolicy:
    """A writable account may only live where no user code runs."""

    def test_sql_only_profile_may_hold_a_writable_account(self):
        profile = _profile(
            "L2_container",
            credentials={"warehouse": WAREHOUSE_READWRITE},
            allowed_task_kinds=["sqlmesh.plan", "sqlmesh.apply", "sqlmesh.query"],
        )

        assert_write_grade_allowed(profile)  # must not raise
        assert warehouse_grade_of(profile) == WAREHOUSE_READWRITE

    @pytest.mark.parametrize(
        "kind",
        [
            "agent.shell",
            "sqlmesh.plan_with_python",
            "sqlmesh.render_macro",
            "dagster.run",
            "dagster.codelocation",
            "sqlmesh.evaluate",
        ],
    )
    def test_a_code_running_profile_may_not(self, kind):
        """The exact combination the L2/L3 split exists to prevent."""
        profile = _profile(
            "L2_container",
            credentials={"warehouse": WAREHOUSE_READWRITE},
            allowed_task_kinds=["sqlmesh.apply", kind],
        )

        with pytest.raises(CredentialPolicyError, match="SQL-only"):
            assert_write_grade_allowed(profile)

    def test_a_code_running_profile_with_a_readonly_account_is_fine(self):
        profile = _profile(
            "L3_base",
            credentials={"warehouse": WAREHOUSE_READONLY},
            allowed_task_kinds=["agent.shell", "sqlmesh.plan_with_python"],
        )

        assert_write_grade_allowed(profile)
        assert warehouse_grade_of(profile) == WAREHOUSE_READONLY

    def test_a_profile_that_says_nothing_gets_nothing(self):
        """Inheriting the caller's wish is how a profile silently gains write access."""
        profile = _profile("L2_container", allowed_task_kinds=["sqlmesh.apply"])

        assert warehouse_grade_of(profile) == "none"

    def test_the_real_profiles_satisfy_the_constraint(self):
        """Guards conf/isolation-profiles.yaml itself, not a fixture."""
        from datacode_common.isolation import load_isolation_policy

        policy = load_isolation_policy(refresh=True)
        for name in policy.profile_names:
            # Must not raise for any declared profile.
            assert_write_grade_allowed(policy.profile(name))


class TestSandboxCredentials:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv(TASK_TOKEN_SECRET_ENV, SECRET)
        from src import config as config_module

        monkeypatch.setattr(config_module.settings, "egress_proxy_url", PROXY)
        import src.tasks.credentials as credentials_module

        monkeypatch.setattr(credentials_module.settings, "egress_proxy_url", PROXY)

    def _profile(self) -> IsolationProfile:
        return _profile(
            "L2_container",
            credentials={"warehouse": WAREHOUSE_READWRITE},
            allowed_task_kinds=["sqlmesh.apply"],
        )

    def test_token_is_bound_to_this_task_only(self):
        spec = _spec()

        credentials = build_sandbox_credentials(spec, self._profile(), secret=SECRET)

        claims = verify_task_token(credentials.task_token, secret=SECRET)
        assert claims.task_id == spec.task_id
        assert claims.tenant_id == spec.tenant_id
        assert claims.workspace_path == spec.workspace_path
        assert claims.isolation_profile == "L2_container"

    def test_token_expires_with_the_task(self):
        spec = _spec(timeout_seconds=300)
        now = int(time.time())

        credentials = build_sandbox_credentials(
            spec, self._profile(), secret=SECRET, now=now
        )

        # The task's own budget plus the flush margin, never more.
        assert now + 300 <= credentials.expires_at <= now + 300 + 300

    def test_proxy_variables_carry_the_token(self):
        """A library configured through HTTPS_PROXY must be able to authenticate."""
        credentials = build_sandbox_credentials(_spec(), self._profile(), secret=SECRET)

        assert credentials.env[ENV_HTTPS_PROXY].startswith("http://")
        assert credentials.task_token in credentials.env[ENV_HTTPS_PROXY]
        # The bare address is exposed separately, without the credential.
        assert credentials.env[ENV_EGRESS_PROXY] == PROXY

    def test_lowercase_proxy_variables_are_set_too(self):
        """Several clients read only the lowercase spelling."""
        env = build_sandbox_credentials(_spec(), self._profile(), secret=SECRET).env

        assert env["https_proxy"] == env[ENV_HTTPS_PROXY]

    def test_no_proxy_exempts_only_loopback(self):
        """Exempting a platform host would bypass every rule the proxy applies."""
        env = build_sandbox_credentials(_spec(), self._profile(), secret=SECRET).env

        assert env["NO_PROXY"] == "localhost,127.0.0.1"

    def test_the_summary_never_contains_a_value(self):
        credentials = build_sandbox_credentials(_spec(), self._profile(), secret=SECRET)

        rendered = str(credentials.safe_summary())

        assert credentials.task_token not in rendered
        assert TASK_TOKEN_ENV in rendered  # the key IS reported

    def test_missing_signing_secret_refuses_the_task(self, monkeypatch):
        """No token means no egress; discovering that later misplaces the cause."""
        monkeypatch.delenv(TASK_TOKEN_SECRET_ENV, raising=False)

        with pytest.raises(CredentialPolicyError, match="no way to authenticate"):
            build_sandbox_credentials(_spec(), self._profile())

    def test_a_preauthenticated_proxy_url_is_stripped(self, monkeypatch):
        """A credential in configuration would be a second one nobody rotates."""
        import src.tasks.credentials as credentials_module

        monkeypatch.setattr(
            credentials_module.settings,
            "egress_proxy_url",
            "http://someone:hunter2@egress:3128",
        )

        env = build_sandbox_credentials(_spec(), self._profile(), secret=SECRET).env

        assert "hunter2" not in env[ENV_HTTPS_PROXY]
        assert env[ENV_HTTPS_PROXY].endswith("@egress:3128")


class TestDerivedEgress:
    """Destinations come from the working copy, so they cannot drift wider."""

    class _Connection:
        DIALECT = "postgres"

        def __init__(self, host="", port=0):
            self.host = host
            self.port = port

    class _Gateway:
        def __init__(self, connection):
            self.connection = connection

    class _Config:
        def __init__(self, gateways, default=""):
            self.gateways = gateways
            self.default_gateway_name = default

    def test_the_default_gateway_becomes_a_destination(self):
        config = self._Config(
            {"prod": self._Gateway(self._Connection("wh.acme.example", 5432))}, "prod"
        )

        destinations, notes = warehouse_destinations_of(config)

        assert len(destinations) == 1
        assert destinations[0].host == "wh.acme.example"
        assert destinations[0].ports == (5432,)
        assert destinations[0].kind == "warehouse"
        assert notes

    def test_only_the_selected_gateway_is_allowed(self):
        """A task connecting to one warehouse has no reason to reach a second."""
        config = self._Config(
            {
                "prod": self._Gateway(self._Connection("prod.example", 5432)),
                "staging": self._Gateway(self._Connection("staging.example", 5432)),
            },
            "prod",
        )

        destinations, _ = warehouse_destinations_of(config)

        assert [d.host for d in destinations] == ["prod.example"]

    def test_a_local_engine_needs_no_egress(self):
        class Duck:
            DIALECT = "duckdb"

        config = self._Config({"local": self._Gateway(Duck())}, "local")

        destinations, notes = warehouse_destinations_of(config)

        assert destinations == ()
        assert any("needs no egress" in note for note in notes)

    def test_a_snowflake_account_becomes_a_hostname(self):
        class Snowflake:
            DIALECT = "snowflake"

            def __init__(self):
                self.account = "acme-prod"

        config = self._Config({"sf": self._Gateway(Snowflake())}, "sf")

        destinations, _ = warehouse_destinations_of(config)

        assert destinations[0].host == "acme-prod.snowflakecomputing.com"

    def test_an_unknown_gateway_yields_nothing(self):
        config = self._Config({"prod": self._Gateway(self._Connection("h", 5432))}, "prod")

        destinations, notes = warehouse_destinations_of(config, gateway="nope")

        assert destinations == ()
        assert any("no gateway" in note for note in notes)

    @pytest.mark.parametrize(
        ("url", "host", "ports"),
        [
            ("https://github.com/org/repo.git", "github.com", (443, 80)),
            ("git@gitlab.example.com:org/repo.git", "gitlab.example.com", (22,)),
            ("ssh://git@git.acme.example:2222/org/repo", "git.acme.example", (2222,)),
            ("https://user:token@git.acme.example/org/repo", "git.acme.example", (443, 80)),
        ],
    )
    def test_git_remotes_of_every_shape(self, url, host, ports):
        destination = git_destination(url)

        assert destination is not None
        assert destination.host == host
        assert destination.ports == ports
        assert destination.kind == "git"

    def test_a_credential_in_the_remote_url_does_not_leak_into_the_destination(self):
        destination = git_destination("https://user:hunter2@git.example/org/repo")

        assert "hunter2" not in str(destination)

    @pytest.mark.parametrize("url", ["", "   ", "not a url"])
    def test_unusable_remotes_yield_nothing(self, url):
        assert git_destination(url) is None


class TestGrantPublication:
    """The submitter's P6b step, asserted through the real submitter."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv(TASK_TOKEN_SECRET_ENV, SECRET)
        import src.tasks.credentials as credentials_module
        import src.tasks.submitter as submitter_module

        monkeypatch.setattr(credentials_module.settings, "egress_proxy_url", PROXY)
        monkeypatch.setattr(submitter_module.settings, "egress_proxy_url", PROXY)
        monkeypatch.setattr(submitter_module.settings, "egress_require_grant", True)
        # Resolve to a fixed destination rather than reading a real project.
        async def fake_resolve(workspace_path, *, gateway="", include_git=True):
            from src.tasks.egress import WorkingCopyEgress

            return WorkingCopyEgress(
                destinations=(
                    EgressDestination(
                        host="wh.acme.example", ports=(5432,), kind="warehouse"
                    ),
                ),
                gateway=gateway or "prod",
            )

        monkeypatch.setattr(submitter_module, "resolve_working_copy_egress", fake_resolve)
        monkeypatch.setattr(
            submitter_module,
            "runtime_isolation_policy",
            lambda refresh=False: build_isolation_policy(
                {
                    "profiles": {
                        "L2_container": {
                            "isolation": "container",
                            "runtime_class": "runc",
                            "timeout_seconds": 3600,
                            "credentials": {"warehouse": WAREHOUSE_READWRITE},
                            "network": {
                                "default": "deny",
                                "denied_cidrs": ["10.0.0.0/8"],
                            },
                            "allowed_task_kinds": ["sqlmesh.apply"],
                        },
                        "L3_base": {
                            "isolation": "container",
                            "runtime_class": "runc",
                            "allowed_task_kinds": ["agent.shell"],
                        },
                    },
                    "default_profile": "strongest_enabled",
                }
            ),
        )

    async def _submit(self, grants: _MemoryGrants, backend: Optional[TaskBackend] = None):
        from src.tasks.submitter import TaskSubmitter

        vehicle = backend or _RecordingBackend()
        submitter = TaskSubmitter(
            backend=vehicle, auditor=_NullAuditor(), grant_store=grants
        )
        result = await submitter.submit(
            "sqlmesh.apply",
            "/data/workspaces/t1/ws1/dev",
            environment="dev",
            tenant_id="tenant-1",
            workspace_id="ws-1",
            timeout_seconds=600,
        )
        return result, vehicle

    async def test_a_grant_is_published_before_the_vehicle_starts(self):
        grants = _MemoryGrants()

        result, backend = await self._submit(grants)

        assert result.succeeded
        assert len(grants.published) == 1
        grant = grants.published[0]
        assert grant.tenant_id == "tenant-1"
        assert [d.host for d in grant.destinations] == ["wh.acme.example"]

    async def test_the_profiles_denied_cidrs_travel_with_the_grant(self):
        """Otherwise they would be declared and never enforced."""
        grants = _MemoryGrants()

        await self._submit(grants)

        assert grants.published[0].denied_cidrs == ("10.0.0.0/8",)

    async def test_the_sandbox_receives_its_credentials(self):
        grants = _MemoryGrants()

        _, backend = await self._submit(grants)

        credentials = backend.credentials[0]
        assert credentials is not None
        claims = verify_task_token(credentials.task_token, secret=SECRET)
        assert claims.tenant_id == "tenant-1"

    async def test_the_grant_is_revoked_when_the_task_ends(self):
        """Closes the window in which a leaked token still works."""
        grants = _MemoryGrants()

        await self._submit(grants)

        assert grants.revoked == [grants.published[0].task_id]

    async def test_an_unpublishable_grant_refuses_the_submission(self):
        """Better than a sandbox that fails on its first warehouse connection."""
        from src.tasks.submitter import TaskSubmissionError

        grants = _MemoryGrants(failing=True)

        with pytest.raises(TaskSubmissionError, match="egress grant"):
            await self._submit(grants)

    async def test_no_proxy_configured_means_no_credentials_and_no_grant(
        self, monkeypatch
    ):
        """A valid state: the task acts only on its working copy."""
        import src.tasks.submitter as submitter_module

        monkeypatch.setattr(submitter_module.settings, "egress_proxy_url", "")
        grants = _MemoryGrants()

        result, backend = await self._submit(grants)

        assert result.succeeded
        assert grants.published == []
        assert backend.credentials == [None]
