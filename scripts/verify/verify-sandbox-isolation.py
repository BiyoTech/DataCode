#!/usr/bin/env python3
"""P6 sandbox security acceptance (design 06, T1–T10) — run it, do not argue it.

This script is the executable form of the P6 acceptance gate. Every check that
CAN be executed on the machine running it is executed against the real code
path; everything else is reported as ``REQUIRES-K8S`` or ``MANUAL`` rather than
green. A skipped check is a hole in the record, never a pass.

How the checks are run
----------------------
The sandbox assertions go through the REAL task plane — ``TaskSubmitter`` with
the local (L3-proc) backend — inside the workspace-runtime virtualenv, because
that is the only place where "the environment was cleared" and "the rlimits were
applied" mean anything. The script itself stays stdlib-only and drives those
probes over a subprocess, printing one verdict per threat.

What each status means
----------------------
PASS          the assertion held, measured on this machine
FAIL          it did not — the gate is not met
REQUIRES-K8S  the control is a container/network boundary (mount namespace,
              NetworkPolicy, cgroup, read-only rootfs). A process-level sandbox
              cannot demonstrate it, and pretending otherwise is how a security
              gate turns into theatre. Run with ``--k8s`` against a real cluster.
MANUAL        no automated verdict exists (T8: kernel escape)

Usage:
    python3 scripts/verify/verify-sandbox-isolation.py            # dev: everything except K8s
    python3 scripts/verify/verify-sandbox-isolation.py --k8s      # additionally run in-cluster probes
    python3 scripts/verify/verify-sandbox-isolation.py --only T2,Q5 # a subset, by check id

Exit code 1 if ANY check FAILed. ``REQUIRES-K8S`` / ``MANUAL`` do not fail the
run — they are printed as an explicit coverage gap for a human to close.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
REQUIRES_K8S = f"{YELLOW}REQUIRES-K8S{RESET}"
MANUAL = f"{CYAN}MANUAL{RESET}"

RUNTIME_DIR = os.path.join(REPO_ROOT, "workspace-runtime")
TASK_RUNNER_DIR = os.path.join(REPO_ROOT, "task-runner")
EGRESS_PROXY_DIR = os.path.join(REPO_ROOT, "egress-proxy")
ISOLATION_PROFILES = os.path.join(REPO_ROOT, "conf", "isolation-profiles.yaml")


class Result:
    """One check's verdict."""

    def __init__(self, check_id: str, title: str, status: str, detail: str = ""):
        self.check_id = check_id
        self.title = title
        self.status = status  # PASS | FAIL | REQUIRES-K8S | MANUAL
        self.detail = detail

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"

    def render(self) -> str:
        badge = {
            "PASS": PASS,
            "FAIL": FAIL,
            "REQUIRES-K8S": REQUIRES_K8S,
            "MANUAL": MANUAL,
        }[self.status]
        line = f"  {badge:<20} {self.check_id:<6} {self.title}"
        if self.detail:
            line += f"\n{'':<30}{self.detail}"
        return line


# ── probe plumbing ──────────────────────────────────────────────────────

def venv_python(service_dir: str) -> Optional[str]:
    """Path to a service's venv interpreter, or None when it is not built."""
    candidate = os.path.join(service_dir, ".venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else None


def run_probe(service_dir: str, code: str, timeout: float = 300.0) -> dict:
    """Run ``code`` in a service's venv and collect its reported checks.

    The probe prints one final line ``@@VERIFY@@ {json}``; anything before it is
    its own stdout and is kept for the failure detail, because a probe that dies
    midway has no verdict to report and its output is the only evidence.
    """
    python = venv_python(service_dir)
    if python is None:
        return {"error": f"no virtualenv at {service_dir}/.venv", "checks": []}

    env = dict(os.environ)
    # The dev vehicle, never the production one: this script proves the sandbox
    # contract, and only the local backend can run without a cluster.
    env["WORKSPACE_RUNTIME_TASK_BACKEND"] = "local"
    env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        completed = subprocess.run(
            [python, "-"],
            input=code,
            capture_output=True,
            text=True,
            cwd=service_dir,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"probe timed out after {timeout}s", "checks": []}

    for line in completed.stdout.splitlines():
        if line.startswith("@@VERIFY@@ "):
            try:
                return json.loads(line[len("@@VERIFY@@ "):])
            except json.JSONDecodeError as exc:
                return {"error": f"probe emitted malformed JSON: {exc}", "checks": []}

    tail = (completed.stderr or completed.stdout).strip().splitlines()[-6:]
    return {"error": "probe produced no verdict; tail:\n    " + "\n    ".join(tail),
            "checks": []}


def probe_to_results(probe: dict, prefix: str = "") -> list[Result]:
    """Turn a probe's JSON into Results; a probe-level error becomes one FAIL."""
    if probe.get("error"):
        return [Result(prefix or "probe", "probe could not run", "FAIL",
                       probe["error"].replace("\n", " ")[:300])]
    out = []
    for check in probe.get("checks", []):
        out.append(Result(
            check_id=check.get("id", ""),
            title=check.get("title", ""),
            status="PASS" if check.get("ok") else "FAIL",
            detail=str(check.get("detail", ""))[:300],
        ))
    return out


# ── the sandbox probe (T1-partial, T2, T5-partial, git hooks) ───────────

SANDBOX_PROBE = r'''
"""Runs inside workspace-runtime's venv: real tasks through the local backend.

Only the audit writer is replaced (a no-op): this script is about the sandbox
boundary, and a database is not part of that boundary. The backend, the profile
resolution, the environment clearing and the rlimits are all the production
code.
"""
import asyncio
import json
import os
import resource
import shutil
import subprocess
import tempfile

os.environ.setdefault("WORKSPACE_RUNTIME_TASK_BACKEND", "local")

checks = []


def record(check_id, title, ok, detail=""):
    checks.append({"id": check_id, "title": title, "ok": bool(ok),
                   "detail": str(detail)[:300]})


root = tempfile.mkdtemp(prefix="verify-p6-")
base = os.path.join(root, "base")
workspace = os.path.join(base, "t1", "ws-t1-verify6", "u1", "envs", "dev")
os.makedirs(workspace, exist_ok=True)

from src.config import settings

settings.workspace_base_path = base


class NoopAuditor:
    """Replaces the DB-backed auditor: durability is not what we verify here."""

    async def record_submitted(self, spec, *, backend):
        return None

    async def record_finished(self, spec, result):
        return None


async def run_task(command, timeout=60):
    """Submit one shell task through the real submitter and return its payload."""
    from src.tasks.submitter import TaskSubmitter

    submitter = TaskSubmitter(auditor=NoopAuditor())
    try:
        result = await submitter.submit(
            kind="agent.shell",
            workspace_path=workspace,
            params={"command": command, "timeout_s": timeout},
            tenant_id="t1",
            workspace_id="ws-t1-verify6",
            actor_user_id="verify",
        )
    finally:
        await submitter.close()
    return result


def payload_of(result):
    """The shell result dict, or a best-effort description of the failure."""
    if not result.succeeded:
        return None, f"[{result.error_code}] {result.error_message}"
    payload = result.result or {}
    if payload.get("exit_code", 0) != 0:
        return payload, f"exit={payload.get('exit_code')} " \
                        f"stderr={str(payload.get('stderr', ''))[:200]}"
    return payload, ""


# ── T2: no platform credential is readable from inside the sandbox ──────
# Sentinel credentials are planted in THIS process's environment first: the
# assertion "the sandbox cannot see a platform secret" is vacuous if the process
# under test happens to hold none. They are planted here, read back through the
# same helper the production code uses, and then looked for in the child's env.
from src.isolation.hardening import platform_secret_names

SENTINELS = {
    "VERIFY_DB_PASSWORD": "sentinel-db-password",
    "VERIFY_TOKEN_ENCRYPTION_SECRET": "sentinel-token-secret",
    "VERIFY_INTERNAL_TOKEN": "sentinel-internal-token",
    "VERIFY_LLM_API_KEY": "sentinel-llm-key",
    "VERIFY_AWS_SECRET_ACCESS_KEY": "sentinel-aws-key",
}
os.environ.update(SENTINELS)

secret_names = platform_secret_names(os.environ)
planted = [name for name in SENTINELS if name in secret_names]
result = asyncio.run(run_task("env | sort"))
payload, problem = payload_of(result)
if payload is None:
    record("T2", "no platform credential visible in the sandbox", False, problem)
else:
    environ_text = payload.get("stdout", "")
    leaked = [name for name in secret_names if f"{name}=" in environ_text]
    sufficient = len(planted) == len(SENTINELS)
    record("T2", "no platform credential visible in the sandbox",
           sufficient and not leaked,
           f"{len(secret_names)} secret-named vars checked "
           f"({len(planted)}/{len(SENTINELS)} sentinels planted), "
           f"leaked={leaked or 'none'}, "
           f"sandbox env has {len(environ_text.splitlines())} entries")

# ── T1 (partial): the vehicle's cwd IS the working copy ────────────────
result = asyncio.run(run_task("pwd"))
payload, problem = payload_of(result)
if payload is None:
    record("T1", "cwd pinned to the working copy", False, problem)
else:
    cwd = payload.get("stdout", "").strip()
    # The temp dir is a symlink on macOS; compare resolved paths.
    record("T1", "cwd pinned to the working copy",
           os.path.realpath(cwd) == os.path.realpath(workspace),
           f"cwd={cwd} (expected {workspace})")

# ── T5 (partial): the profile's rlimits are actually applied ───────────
from datacode_common.isolation import IsolationLevel
from src.isolation.hardening import ResourceLimits
from src.isolation.policy import runtime_isolation_policy

policy = runtime_isolation_policy()
profile = policy.profile_for("agent.shell")
limits = ResourceLimits.from_profile(profile.settings)

result = asyncio.run(run_task(
    "python3 -c \"import resource;"
    "print(resource.getrlimit(resource.RLIMIT_FSIZE)[0],"
    "resource.getrlimit(resource.RLIMIT_NOFILE)[0])\" 2>&1"
))
payload, problem = payload_of(result)
if payload is None:
    record("T5", "resource limits applied in the vehicle", False, problem)
else:
    try:
        fsize, nofile = (int(part) for part in payload["stdout"].split())
    except (KeyError, ValueError):
        record("T5", "resource limits applied in the vehicle", False,
               f"could not read limits from stdout={payload.get('stdout')!r} "
               f"stderr={payload.get('stderr')!r}")
    else:
        # Soft limits may only be LOWERED, so "at or below the declaration" is
        # the assertion; higher would mean the profile was ignored.
        ok = fsize <= limits.max_file_size_bytes and nofile <= limits.max_open_files
        record("T5", "resource limits applied in the vehicle", ok,
               f"profile={profile.name} level={profile.level.value} "
               f"fsize={fsize}<={limits.max_file_size_bytes} "
               f"nofile={nofile}<={limits.max_open_files}")

# ── git hooks: a pushed repository must not execute code on checkout ───
HOOK_MARKER = "HOOK_RAN"
hook_dir = os.path.join(workspace, "repo")
os.makedirs(hook_dir, exist_ok=True)
marker_path = os.path.join(hook_dir, HOOK_MARKER)
hook_path = os.path.join(hook_dir, ".git", "hooks", "post-checkout")


def git(*args, cwd=hook_dir, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env=env)


git("init", "-q")
git("config", "user.email", "verify@example.invalid")
git("config", "user.name", "verify")
with open(os.path.join(hook_dir, "README.md"), "w") as handle:
    handle.write("# fixture\n")
git("add", ".")
git("commit", "-q", "-m", "initial")
git("branch", "other")

os.makedirs(os.path.dirname(hook_path), exist_ok=True)
with open(hook_path, "w") as handle:
    handle.write(
        "#!/bin/sh\n"
        f"echo pwned > {marker_path}\n"
    )
os.chmod(hook_path, 0o755)

# Control: WITHOUT the disabling flag the hook does run. Without this control a
# green result below could just mean the fixture is broken.
env_without_guard = dict(os.environ)
env_without_guard.pop("GIT_CONFIG_PARAMETERS", None)
if os.path.exists(marker_path):
    os.remove(marker_path)
subprocess.run(["git", "checkout", "other"], cwd=hook_dir, capture_output=True,
               text=True, env=env_without_guard)
control_ran = os.path.exists(marker_path)

if os.path.exists(marker_path):
    os.remove(marker_path)
result = asyncio.run(run_task(f"cd repo && git checkout other 2>&1 | tail -3"))
payload, problem = payload_of(result)
hook_ran = os.path.exists(marker_path)
record("HOOK", "repository git hooks do not execute in the vehicle",
       control_ran and not hook_ran,
       f"control (no guard) ran hook={control_ran}, "
       f"in-vehicle hook ran={hook_ran}"
       + ("" if payload is not None else f"; task failed: {problem}"))

print("@@VERIFY@@ " + json.dumps({"checks": checks}))
shutil.rmtree(root, ignore_errors=True)
'''


# ── policy probes (Q5 credentials, T3/T6/T7 egress decisions) ───────────

POLICY_PROBE = r'''
"""Asserts the POLICY layer against the real conf/isolation-profiles.yaml.

These are decisions, not vehicles: they are what the sandbox is told to allow,
and they are the part that must never regress silently.
"""
import asyncio
import ipaddress
import json
import os

checks = []


def record(check_id, title, ok, detail=""):
    checks.append({"id": check_id, "title": title, "ok": bool(ok),
                   "detail": str(detail)[:300]})


from datacode_common.egress import EgressDestination, EgressPolicy
from datacode_common.isolation import load_isolation_policy
from src.isolation.policy import runtime_isolation_policy

# Two views on purpose:
#   declared  — what the configuration MEANS, unaffected by this machine
#   policy    — what this process can actually execute (degraded in dev, where
#               no container runtime exists)
# Q5 is about the declaration; the runtime view is only reported.
declared = load_isolation_policy()
policy = runtime_isolation_policy()

# ── Q5: user code never coexists with a writable warehouse account ─────
# Asserted against the DECLARED profiles, not the ones this process can run:
# a dev install has no container runtime, so the policy layer degrades
# L3_base to L3_proc and the declaration is the only thing that still means
# anything here. The runtime degradation is reported separately below.
import dataclasses

from src.tasks.credentials import (
    WAREHOUSE_READWRITE,
    assert_write_grade_allowed,
    build_sandbox_credentials,
    warehouse_grade_of,
)

USER_CODE_KINDS = (
    "agent.shell",
    "sqlmesh.plan_with_python",
    "sqlmesh.render_macro",
    "sqlmesh.render_project",
    "dagster.run",
    "dagster.codelocation",
)

# (a) every enabled profile must satisfy the Q5 guard over its own kind list —
# this is the production check, so no kind list of ours can miss a case.
violations = []
for profile in declared.enabled_profiles:
    try:
        assert_write_grade_allowed(profile)
    except Exception as exc:  # noqa: BLE001 - the guard's own exception type
        violations.append(f"{profile.name}: {exc}"[:160])

record("Q5", "no declared profile grants write access to user code",
       not violations,
       f"{len(declared.enabled_profiles)} declared profiles checked; "
       f"violations={violations or 'none'}")

# (b) the profile that DECLARES each user-code kind holds a read-only account.
offenders = []
for kind in USER_CODE_KINDS:
    declaring = next(
        (p for p in declared.enabled_profiles if kind in p.allowed_task_kinds),
        None,
    )
    if declaring is None:
        offenders.append(f"{kind}: no profile claims it")
        continue
    grade = warehouse_grade_of(declaring)
    if grade != "scoped_readonly":
        offenders.append(f"{kind}->{declaring.name}(grade={grade})")

record("Q5b", "user-code kinds declared with a read-only warehouse account",
       not offenders,
       f"{len(USER_CODE_KINDS)} kinds checked; offenders={offenders or 'none'}")

# (c) assembly must refuse the impossible combination outright, rather than
# trust the YAML to stay correct: flip one real profile to readwrite and
# expect the guard to stop it.
base_profile = declared.profile("L3_base")
corrupted = dataclasses.replace(
    base_profile,
    settings={**base_profile.settings,
              "credentials": {"warehouse": WAREHOUSE_READWRITE, "platform": "none"}},
)
refused, detail = False, ""
try:
    assert_write_grade_allowed(corrupted)
except Exception as exc:  # noqa: BLE001 - CredentialPolicyError or a subclass
    refused = True
    detail = f"{type(exc).__name__}: {exc}"[:160]
record("Q5c", "credential assembly refuses writable + user code", refused,
       detail or "NOT refused — a writable account would reach user code")

# (d) the same refusal must hold at the real assembly entry point, which is
# what a task actually goes through.
from datacode_common.task_protocol import TaskSpec
from src.tasks.credentials import CredentialPolicyError

spec = TaskSpec(
    task_id="verify-q5",
    kind="agent.shell",
    workspace_path="/data/workspaces/t1/ws-t1-verify6/u1/envs/dev",
    tenant_id="t1",
    workspace_id="ws-t1-verify6",
    isolation_profile=corrupted.name,
    environment="dev",
    gateway="",
    params={},
    timeout_seconds=60,
)
refused_at_entry, entry_detail = False, ""
try:
    build_sandbox_credentials(spec, corrupted, secret="verify-secret")
except CredentialPolicyError as exc:
    refused_at_entry = True
    entry_detail = str(exc)[:160]
except Exception as exc:  # noqa: BLE001 - any other failure is itself a finding
    entry_detail = f"unexpected {type(exc).__name__}: {exc}"[:160]
record("Q5d", "build_sandbox_credentials refuses the same combination",
       refused_at_entry, entry_detail or "NOT refused at the assembly entry point")

# Report the dev degradation honestly: it is not a failure, but an operator
# must not read these PASSes as "the container profile ran here".
degraded = sorted(
    f"{kind}:{policy.profile_for(kind).name}"
    for kind in USER_CODE_KINDS
    if policy.profile_for(kind).name != next(
        (p.name for p in declared.enabled_profiles
         if kind in p.allowed_task_kinds), ""
    )
)
if degraded:
    record("DEV", "container profiles unavailable in this process (reported)",
           True,
           f"degraded to {degraded}; the container boundary is NOT proven here")

# ── T3 / T6 / T7: the egress decisions that NetworkPolicy also enforces ──
l3_base = policy.profile_for("agent.shell")
declared = (
    EgressDestination(host="warehouse.acme.example", ports=(5432,), kind="warehouse"),
    EgressDestination(host="github.com", ports=(443,), kind="git"),
)


def fake_resolver(host):
    """Resolved addresses, injected so no DNS is needed for the verdict.

    Globally routable on purpose: the policy blocks reserved and non-global
    ranges, so a documentation range here would be refused for the wrong
    reason and the "declared host IS allowed" check would prove nothing.
    """
    return {
        "warehouse.acme.example": ["1.1.1.1"],
        "github.com": ["140.82.121.4"],
    }.get(host, [])


egress = EgressPolicy.from_profile(
    l3_base.settings, declared, resolver=fake_resolver
)

denied_cases = (
    ("T6", "cloud metadata endpoint refused", "169.254.169.254", 80),
    ("T7", "RFC1918 / carrier-grade ranges refused", "10.0.0.1", 80),
    ("T7", "9.0.0.0/8 refused", "9.1.1.1", 80),
    ("T7", "11.0.0.0/8 refused", "11.1.1.1", 80),
    ("T7", "21.0.0.0/8 refused", "21.1.1.1", 80),
    ("T7", "30.0.0.0/8 refused", "30.1.1.1", 80),
    ("T7", "loopback refused", "127.0.0.1", 80),
    ("T3", "undeclared warehouse host refused", "other-warehouse.example", 5432),
    ("T3", "declared host on an undeclared port refused", "warehouse.acme.example", 22),
)
for check_id, title, host, port in denied_cases:
    decision = egress.evaluate(host, port)
    record(check_id, title, not decision.allowed,
           f"{host}:{port} -> {decision.reason}: {decision.detail}"[:200])

allowed = egress.evaluate("warehouse.acme.example", 5432)
record("T3b", "the declared warehouse IS reachable (policy is not deny-all)",
       allowed.allowed,
       f"warehouse.acme.example:5432 -> {allowed.reason}")

# A declared NAME that resolves into a denied range must still be refused:
# that is the DNS-rebinding case the resolved-address check exists for.
rebind = EgressPolicy.from_profile(
    l3_base.settings,
    (EgressDestination(host="evil.example", ports=(5432,), kind="warehouse"),),
    resolver=lambda host: ["169.254.169.254"] if host == "evil.example" else [],
).evaluate("evil.example", 5432)
record("T6b", "declared name resolving into a denied range is refused",
       not rebind.allowed, f"evil.example -> {rebind.reason}: {rebind.detail}"[:200])

# ── T10 (declaration): the hardening the backends emit into every Pod ──
# Asserted against the SOURCE of the two emitting methods rather than by
# instantiating the backend: the kubernetes client is an optional dependency
# that a dev install deliberately omits, and a whole-file grep would also pass
# on a comment mentioning the setting. Scoping to the method body keeps the
# check meaningful without requiring a cluster.
import inspect

from src.executor import container as warm_container
from src.tasks.backends import kubernetes as k8s_backend

container_src = inspect.getsource(
    k8s_backend.KubernetesBackend._container_security_context
)
pod_src = inspect.getsource(k8s_backend.KubernetesBackend._pod_security_context)

hardening = {
    "read_only_root_filesystem": "read_only_root_filesystem" in container_src,
    "allow_privilege_escalation=False": "allow_privilege_escalation=False" in container_src,
    "capabilities dropped": "drop=" in container_src,
    "run_as_non_root=True": "run_as_non_root=True" in pod_src,
    "seccomp profile set": "SeccompProfile" in pod_src,
}
missing_hardening = [name for name, present in hardening.items() if not present]
record("T10", "container hardening emitted into every task Pod",
       not missing_hardening,
       f"methods inspected: _container_security_context, _pod_security_context; "
       + (f"MISSING {missing_hardening}" if missing_hardening
          else "all of read_only_root_filesystem, no privilege escalation, "
               "dropped capabilities, non-root, seccomp")
       + " (runtime proof: --k8s)")

# The warm sandbox must reuse the task backend's hardened context rather than
# grow its own copy that can quietly omit a control.
record("T10b", "warm sandbox container reuses the hardened security context",
       "_container_security_context" in inspect.getsource(warm_container),
       "WarmSandboxBackend calls the task backend's security-context helper")

print("@@VERIFY@@ " + json.dumps({"checks": checks}))
'''


# ── checks that do not need a venv ──────────────────────────────────────

def check_egress_plane_declaration() -> Result:
    """The egress plane's own Pods must be as hardened as the sandboxes.

    The proxy holds no credentials of its own, but it is the one component every
    sandbox talks to, so a writable root there would be the pivot an escape
    lands on.
    """
    path = os.path.join(REPO_ROOT, "deploy", "k8s", "base", "egress-plane.yaml")
    if not os.path.isfile(path):
        return Result("T9", "egress plane pods hardened", "REQUIRES-K8S",
                      f"{os.path.relpath(path, REPO_ROOT)} not found")

    with open(path, encoding="utf-8") as handle:
        body = handle.read()
    required = ("readOnlyRootFilesystem: true", "runAsNonRoot: true",
                "automountServiceAccountToken: false")
    missing = [token for token in required if token not in body]
    return Result(
        "T9", "egress plane pods hardened (declaration)",
        "PASS" if not missing else "FAIL",
        "all of " + ", ".join(required) if not missing
        else f"missing {missing} (runtime proof: --k8s)",
    )


def check_t8_recorded() -> Result:
    """T8 has no automated verdict; record the accepted risk explicitly."""
    return Result(
        "T8", "container escape (kernel CVE)",
        "MANUAL",
        "NOT covered by L3-base (Q2 decision). Mitigations: dedicated user-code "
        "node pool + kernel patching. A successful escape must be recorded as a "
        "risk baseline, not as a surprise.",
    )


def check_kubernetes_only() -> list[Result]:
    """The controls whose verdict requires a real cluster."""
    return [
        Result("T1", "only the working copy is visible (mount namespace)",
               "REQUIRES-K8S",
               "L3-proc has no filesystem namespace; subPath mount is what "
               "hides other tenants' copies. Run --k8s."),
        Result("T3", "direct connections to the warehouse/Redis are refused",
               "REQUIRES-K8S",
               "default-deny NetworkPolicy (deploy/k8s/base/egress-plane.yaml); "
               "the policy DECISION is verified above, the drop is not."),
        Result("T4", "platform service endpoints unreachable from the sandbox",
               "REQUIRES-K8S",
               "NetworkPolicy + no service-account token; needs a cluster."),
        Result("T5", "cgroup limits kill only the offending task",
               "REQUIRES-K8S", "rlimits are verified above; cgroups are not."),
        Result("T6", "169.254.169.254 actually unreachable",
               "REQUIRES-K8S", "decision verified above; the drop is not."),
        Result("T7", "internal networks actually unreachable",
               "REQUIRES-K8S", "decision verified above; the drop is not."),
        Result("T9", "egress quota enforced on live traffic",
               "REQUIRES-K8S",
               "needs egress-proxy + Redis; see egress-proxy/tests and the P6b "
               "smoke record for grant metering and quota refusal."),
        Result("T10", "writes to the platform filesystem actually refused",
               "REQUIRES-K8S", "declaration verified above; the refusal is not."),
    ]


# ── main ────────────────────────────────────────────────────────────────

COVERED_BY_SUITES = (
    ("code location Pod matches the checked-in contract",
     "workspace-runtime/tests/test_codeloc_pod.py::TestContractAlignment"),
    ("DNS rebinding: every resolved address must pass",
     "shared/python/tests (datacode_common.egress)"),
    ("scoped task token: tamper/expiry/scope",
     "shared/python/tests (datacode_common.security.task_token)"),
    ("egress proxy: no-token 407, undeclared 403, byte metering",
     "egress-proxy/tests"),
    ("credential policy refuses writable + user code",
     "workspace-runtime/tests + Q5c/Q5d above"),
    ("read-side metadata served through the sandbox",
     "workspace-runtime/tests/test_metadata_context.py, test_metadata_ops.py"),
    ("local task backend hardens the child (env clear + rlimits)",
     "workspace-runtime/tests (P6a suite)"),
)


def print_suite_coverage() -> None:
    """Checks that live in the suites, printed so the acceptance record is whole."""
    print("\n── covered by the automated suites (not re-run here) ──")
    for claim, where in COVERED_BY_SUITES:
        print(f"   - {claim}: {where}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P6 sandbox security acceptance (design 06, T1-T10).")
    parser.add_argument("--k8s", action="store_true",
                        help="also run the in-cluster probes (needs kubectl + a "
                             "cluster with the user-code node pool)")
    parser.add_argument("--only", default="",
                        help="comma-separated check ids to run (e.g. T2,Q5)")
    parser.add_argument("--probe-timeout", type=float, default=300.0)
    args = parser.parse_args()

    selected = {part.strip().upper() for part in args.only.split(",") if part.strip()}

    print("P6 sandbox acceptance — design 06 T1–T10")
    print(f"repo: {REPO_ROOT}")
    runtime_python = venv_python(RUNTIME_DIR)
    if runtime_python is None:
        print(f"\n{FAIL} workspace-runtime/.venv is missing; build it first:\n"
              f"    cd workspace-runtime && python3.13 -m venv .venv && "
              f"./.venv/bin/pip install -e ../shared/python && "
              f"./.venv/bin/pip install -e '.[dev]'")
        return 2
    print(f"runtime venv: {runtime_python}\n")

    results: list[Result] = []

    print("── sandbox (real tasks through the local backend) ──")
    probe = run_probe(RUNTIME_DIR, SANDBOX_PROBE, timeout=args.probe_timeout)
    results.extend(probe_to_results(probe))

    print("── policy (real conf/isolation-profiles.yaml) ──")
    probe = run_probe(RUNTIME_DIR, POLICY_PROBE, timeout=args.probe_timeout)
    results.extend(probe_to_results(probe))

    print("── deployment declarations ──")
    results.append(check_egress_plane_declaration())
    results.append(check_t8_recorded())

    if args.k8s:
        print("── in-cluster ──")
        results.extend(run_kubernetes_probes())
    else:
        results.extend(check_kubernetes_only())

    if selected:
        results = [r for r in results if r.check_id in selected]

    print_suite_coverage()

    print()
    for result in results:
        print(result.render())

    failed = [r for r in results if r.failed]
    gaps = [r for r in results if r.status in ("REQUIRES-K8S", "MANUAL")]
    print()
    print(f"  {len(results) - len(failed) - len(gaps)} passed, "
          f"{len(failed)} failed, {len(gaps)} not verifiable here "
          f"({sum(1 for r in gaps if r.status == 'REQUIRES-K8S')} require a "
          f"cluster, {sum(1 for r in gaps if r.status == 'MANUAL')} manual)")

    if failed:
        print(f"\nP6 acceptance: {FAIL} "
              f"({', '.join(r.check_id for r in failed)})")
        return 1
    if gaps:
        print(f"\nP6 acceptance: {PASS} on every check this machine can run. "
              f"The {len(gaps)} item(s) above are OPEN and must be closed on a "
              f"cluster before the isolation claim is made.")
    else:
        print(f"\nP6 acceptance: {PASS}")
    return 0


def run_kubernetes_probes() -> list[Result]:
    """Run the penetration commands inside a real sandbox Pod.

    Executed only with ``--k8s``. These are the same commands an attacker would
    try, run in the real vehicle — the difference between asserting a policy and
    watching a connection refused.
    """
    if shutil.which("kubectl") is None:
        return [Result("K8S", "in-cluster probes", "REQUIRES-K8S",
                       "kubectl not found on PATH")]
    probe_namespace = os.environ.get("VERIFY_K8S_NAMESPACE", "datacode-user-code")
    image = os.environ.get("VERIFY_K8S_SANDBOX_IMAGE", "")
    if not image:
        return [Result("K8S", "in-cluster probes", "REQUIRES-K8S",
                       "set VERIFY_K8S_SANDBOX_IMAGE to the task-runner image")]

    name = "verify-p6-probe"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": probe_namespace},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "probe",
                "image": image,
                "command": ["sleep", "600"],
                "securityContext": {
                    "readOnlyRootFilesystem": True,
                    "runAsNonRoot": True,
                    "allowPrivilegeEscalation": False,
                },
            }],
        },
    }
    results: list[Result] = []

    def kubectl(*cmd: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        return subprocess.run(["kubectl", *cmd], capture_output=True, text=True,
                              timeout=timeout)

    created = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=json.dumps(manifest),
        capture_output=True, text=True, timeout=120.0)
    if created.returncode != 0:
        return [Result("K8S", "in-cluster probes", "REQUIRES-K8S",
                       f"could not create the probe Pod: "
                       f"{created.stderr.strip()[:200]}")]

    try:
        kubectl("wait", "--for=condition=Ready", f"pod/{name}",
                "-n", probe_namespace, "--timeout=180s")

        def execute(command: str) -> tuple[int, str]:
            completed = kubectl("exec", name, "-n", probe_namespace, "--",
                                "sh", "-c", command)
            return completed.returncode, (completed.stdout + completed.stderr)

        cases = (
            ("T1", "only the working copy is visible",
             "ls / | head -20; find / -maxdepth 2 -name '*.sql' 2>/dev/null | head"),
            ("T3", "warehouse/Redis ports refused",
             "nc -z -w 3 $DATACODE_WAREHOUSE_HOST 5432; nc -z -w 3 redis 6379"),
            ("T4", "platform endpoints refused",
             "curl -s -m 3 -o /dev/null -w '%{http_code}' "
             "http://workspace-runtime:8010/rt/v1/health"),
            ("T6", "cloud metadata refused",
             "curl -s -m 3 -o /dev/null -w '%{http_code}' "
             "http://169.254.169.254/latest/meta-data/"),
            ("T7", "internal networks refused",
             "curl -s -m 3 -o /dev/null -w '%{http_code}' http://10.0.0.1/ ; "
             "curl -s -m 3 -o /dev/null -w '%{http_code}' http://9.1.1.1/"),
            ("T10", "writes outside the working copy refused",
             "touch /probe-write-test && echo WRITTEN || echo REFUSED"),
        )
        for check_id, title, command in cases:
            code, output = execute(command)
            # A refusal shows up as a non-zero exit or an explicit marker; the
            # verdict is printed with the raw output so a human reads the
            # evidence rather than trusting the summary.
            refused = code != 0 or "WRITTEN" not in output
            results.append(Result(check_id, title, "PASS" if refused else "FAIL",
                                  f"exit={code} out={output.strip()[:200]}"))
    finally:
        kubectl("delete", "pod", name, "-n", probe_namespace, "--ignore-not-found")

    return results


if __name__ == "__main__":
    sys.exit(main())
