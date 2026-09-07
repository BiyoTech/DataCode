#!/usr/bin/env python3
"""Configuration doctor — print the EFFECTIVE config and check its invariants.

Why this exists
---------------
Configuration defects in this platform share one property: they do not fail at
the point of the mistake. A profile that was never selected still boots; a
mounted ConfigMap that resolves to the wrong host still connects; a missing
secret disables authorization instead of reporting itself. By the time a symptom
appears it is three layers from its cause.

This tool moves that class of failure to the left. It answers two questions
without touching any external system:

  1. "What configuration will this service actually use?"  (``--profile``)
  2. "Is anything about it structurally wrong?"            (invariant checks)

How the values are produced
---------------------------
By running the REAL loader with ``DATACODE_ENV`` set — never by re-implementing
the resolution rules. A second implementation of "how config resolves" would
drift from the one that matters, and would then confidently print a value the
services do not use. The loader's own module-level cache is reset between
profiles so each one is resolved from scratch.

Usage
-----
    python3 scripts/config-doctor.py                 # every profile, checks only
    python3 scripts/config-doctor.py --profile dev   # one profile, full detail
    python3 scripts/config-doctor.py --show          # all profiles, full detail
    python3 scripts/config-doctor.py --profile k8s-cell --check-secrets

Exit codes
----------
    0  no errors (warnings may be present)
    1  at least one error-severity finding

Secrets are never printed. Passwords are replaced by ``***`` and the tool only
ever reports whether a variable is SET, never its value.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CONF_DIR = REPO_ROOT / "conf"
SHARED_PYTHON = REPO_ROOT / "shared" / "python"

# Every profile the platform ships. Kept here rather than globbed so a stray
# file (platform.dev.yaml.bak) cannot become a "profile", and so a MISSING
# profile is reported instead of silently absent.
PROFILES = ("dev", "docker", "cell", "global", "k8s-cell", "k8s-global")

# Profiles whose addresses are resolved from inside a container: "localhost"
# there is the container itself, not the intended peer.
CONTAINER_PROFILES = frozenset({"docker", "cell", "global", "k8s-cell", "k8s-global"})

# Profiles that run only the global tier. They legitimately publish no gateway,
# runtime or control address.
IDENTITY_ONLY_PROFILES = frozenset({"global", "k8s-global"})

# The frozen platform port assignment. Only the HOST part of a service URL is
# environment-specific, so a differing port is a mistake rather than a choice.
PLATFORM_PORTS = {
    "gateway": 8000,
    "agent": 8001,
    "runtime": 8002,
    "identity": 8003,
    "control": 8004,
}

# Secrets, and who breaks without them. `required` means "the platform cannot
# serve its purpose without it"; the rest degrade a specific capability, which
# is a warning because plenty of valid deployments do not use that capability.
@dataclass(frozen=True)
class SecretSpec:
    var: str
    required: bool
    consumers: str
    without_it: str


SECRETS = (
    SecretSpec(
        "DB_PASSWORD", True, "all DB-backed services",
        "every service fails to connect; a wrong-but-present value crash-loops "
        "in ways that look like anything but an auth failure",
    ),
    SecretSpec(
        "GW_JWT_SECRET", True, "gateway + identity-service",
        "identity cannot mint tokens and the gateway cannot verify them",
    ),
    SecretSpec(
        "IDENTITY_SERVICE_INTERNAL_TOKEN", True, "identity + gateway + control",
        "identity's /internal/* is disabled AND the gateway denies every "
        "authorization-requiring route (fail closed, by design)",
    ),
    SecretSpec(
        "WORKSPACE_RUNTIME_INTERNAL_TOKEN", True, "runtime + gateway + agent + sync",
        "the env-control channel, lease placement/accept/drain and code-location "
        "inventory all reject their callers; agent-service additionally loses "
        "lease placement and falls back to degraded routing",
    ),
    SecretSpec(
        "CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN", True, "control-service",
        "control-service cannot dispatch any filesystem or engine side effect",
    ),
    SecretSpec(
        "DATACODE_TASK_TOKEN_SECRET", False, "runtime + egress-proxy",
        "sandbox egress tokens cannot be minted or verified (only matters where "
        "the egress plane is deployed)",
    ),
    SecretSpec(
        "REDIS_PASSWORD", False, "all services using Redis",
        "nothing, unless your Redis uses requirepass",
    ),
    SecretSpec(
        "CONTROL_SERVICE_CELL_MIGRATION_TOKEN", False, "control-service",
        "the cell-migration endpoints stay disabled (fail closed), so a "
        "migration cannot be driven by accident",
    ),
    SecretSpec(
        "AGENT_SERVICE_LLM_API_KEY", False, "agent-service",
        "every LLM call fails at runtime (per-model keys may cover this)",
    ),
)

_PASSWORD_IN_URL = re.compile(r"(?<=:)[^:@/]+(?=@)")

# Secrets the local flows source from persisted truth files rather than the
# shell (see scripts/dev-lib.sh::ensure_pg_password and ensure_jwt_secret).
# The doctor treats a truth file as a valid supply, so `--check-secrets` does
# not tell a healthy local setup to go set a variable it should not use.
TRUTH_FILES = {
    "DB_PASSWORD": REPO_ROOT / ".data" / "postgres_password",
    # The Makefile injects this into every compose invocation, and
    # dev-start.sh exports it from the same file.
    "GW_JWT_SECRET": REPO_ROOT / ".data" / "gateway_jwt_secret",
}

GREEN, RED, YELLOW, CYAN, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
)


def _redact(url: str) -> str:
    """Strip the password from a connection URL so it is safe to print."""
    return _PASSWORD_IN_URL.sub("***", url)


@dataclass(frozen=True)
class ResolvedConfig:
    """Effective configuration of one profile, as the loader produced it."""

    profile: str
    config_files: tuple[str, ...]
    database_url: str            # redacted
    database_target: str         # host:port/name, for conflict checks
    identity_database_target: Optional[str]
    expect_global_db: bool
    redis_enabled: bool
    redis_url: str               # redacted
    service_urls: dict[str, str]
    workspace_base_path: str
    egress_proxy_url: str
    log_level: str


@dataclass
class Finding:
    """One invariant violation. Severity drives the exit code."""

    severity: str                # "error" | "warning"
    code: str
    message: str
    location: str

    def render(self) -> str:
        colour = RED if self.severity == "error" else YELLOW
        label = "ERROR" if self.severity == "error" else "WARN "
        return (
            f"  {colour}{label}{RESET} {CYAN}{self.code}{RESET}\n"
            f"        {self.message}\n"
            f"        {DIM}at {self.location}{RESET}"
        )


@dataclass
class Report:
    configs: list[ResolvedConfig] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


# ── Resolution ────────────────────────────────────────────────────────────


def resolve(profile: str) -> ResolvedConfig:
    """Load *profile* through the production loader and snapshot the result."""
    # Imported here, after sys.path is prepared by main(), so the module-level
    # import cannot fail before the error message explaining how to fix it.
    import datacode_common.config.platform as platform_module

    previous = os.environ.get("DATACODE_ENV")
    os.environ["DATACODE_ENV"] = profile
    try:
        # Reset the singleton: without this the first profile answers for all.
        platform_module._platform = None
        cfg = platform_module.platform_config()

        identity_spec = cfg.database.services.get("identity")
        files = ["conf/platform.yaml", f"conf/platform.{profile}.yaml"]

        return ResolvedConfig(
            profile=profile,
            config_files=tuple(f for f in files if (REPO_ROOT / f).exists()),
            database_url=_redact(cfg.database.url()),
            database_target=(
                f"{cfg.database.host}:{cfg.database.port}/{cfg.database.name}"
            ),
            identity_database_target=(
                f"{identity_spec.host}:{identity_spec.port}/{identity_spec.name}"
                if identity_spec
                else None
            ),
            expect_global_db=cfg.cells.expect_global_db,
            redis_enabled=cfg.redis.enabled,
            redis_url=cfg.redis.safe_url() if cfg.redis.enabled else "(disabled)",
            service_urls={
                "gateway": cfg.services.url("gateway", "http_url"),
                "agent": cfg.services.url("agent", "rest_url"),
                "agent-grpc": cfg.services.url("agent", "grpc_url"),
                "runtime": cfg.services.url("runtime"),
                "control": cfg.services.url("control"),
                "identity": cfg.services.url("identity"),
            },
            workspace_base_path=cfg.workspace_base_path,
            egress_proxy_url=cfg.services.url("egress_proxy", "url"),
            log_level=cfg.log_level,
        )
    finally:
        platform_module._platform = None
        if previous is None:
            os.environ.pop("DATACODE_ENV", None)
        else:
            os.environ["DATACODE_ENV"] = previous


# ── Invariant checks ─────────────────────────────────────────────────────


def check_profile_files() -> list[Finding]:
    """Every documented profile must have a file, and vice versa."""
    findings: list[Finding] = []
    for profile in PROFILES:
        path = CONF_DIR / f"platform.{profile}.yaml"
        if not path.exists():
            findings.append(Finding(
                "error", "PROFILE_FILE_MISSING",
                f"profile {profile!r} is documented but conf/platform.{profile}.yaml "
                "does not exist; selecting it silently leaves the base defaults in place",
                f"conf/platform.{profile}.yaml",
            ))

    known = {f"platform.{p}.yaml" for p in PROFILES}
    for path in sorted(CONF_DIR.glob("platform.*.yaml")):
        if path.name not in known:
            findings.append(Finding(
                "warning", "PROFILE_FILE_UNKNOWN",
                f"{path.name} looks like a profile but is not one of "
                f"{'|'.join(PROFILES)}; nothing will ever load it",
                f"conf/{path.name}",
            ))
    return findings


def check_base_is_address_free() -> list[Finding]:
    """The base must carry no reachable address.

    An address here serves any deployment that forgot to select a profile,
    turning a startup failure into a silent connection to the wrong place.
    """
    import yaml

    base = yaml.safe_load((CONF_DIR / "platform.yaml").read_text(encoding="utf-8")) or {}
    findings: list[Finding] = []

    for dotted in ("database.host", "redis.host", "workspace.base_path", "services"):
        node = base
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node:
            findings.append(Finding(
                "error", "BASE_DECLARES_ADDRESS",
                f"conf/platform.yaml declares {dotted}={node!r}; addresses belong "
                "in a profile so that a deployment without one fails instead of "
                "connecting somewhere arbitrary",
                "conf/platform.yaml",
            ))
    return findings


def check_addresses(cfg: ResolvedConfig) -> list[Finding]:
    """Each profile must fully address what its tier actually runs."""
    findings: list[Finding] = []
    where = f"conf/platform.{cfg.profile}.yaml"

    if not cfg.database_target.split(":")[0]:
        findings.append(Finding(
            "error", "DATABASE_UNADDRESSED",
            "no database host resolved; the profile is incomplete", where,
        ))

    if cfg.profile in CONTAINER_PROFILES:
        for what, value in [
            ("database", cfg.database_target),
            ("redis", cfg.redis_url if cfg.redis_enabled else ""),
            *cfg.service_urls.items(),
        ]:
            if value and ("localhost" in value or "127.0.0.1" in value):
                findings.append(Finding(
                    "error", "CONTAINER_PROFILE_USES_LOCALHOST",
                    f"{what} resolves to {value!r}; inside a container that is "
                    "the container itself, never the intended peer",
                    where,
                ))

    expected = (
        ["identity"] if cfg.profile in IDENTITY_ONLY_PROFILES
        else ["gateway", "agent", "runtime", "control", "identity"]
    )
    for service in expected:
        if not cfg.service_urls.get(service):
            findings.append(Finding(
                "error", "SERVICE_URL_MISSING",
                f"{service} has no URL; callers will dial an empty upstream and "
                'report `unsupported protocol scheme ""` per request',
                where,
            ))

    if cfg.profile in IDENTITY_ONLY_PROFILES:
        for service in ("gateway", "runtime", "control"):
            if cfg.service_urls.get(service):
                findings.append(Finding(
                    "warning", "SERVICE_URL_UNREACHABLE",
                    f"{service} is published but nothing in the global tier can "
                    "reach one",
                    where,
                ))

    for service, port in PLATFORM_PORTS.items():
        url = cfg.service_urls.get(service, "")
        if url and not url.endswith(f":{port}"):
            findings.append(Finding(
                "error", "PORT_CONTRACT_VIOLATION",
                f"{service} is published as {url!r}, but its platform port is "
                f"{port}; only the host part may differ between profiles",
                where,
            ))
    return findings


def check_global_cell_split(cfg: ResolvedConfig) -> list[Finding]:
    """The flag and the override must agree, in both directions."""
    findings: list[Finding] = []
    where = f"conf/platform.{cfg.profile}.yaml"

    if cfg.expect_global_db and cfg.identity_database_target is None:
        findings.append(Finding(
            "error", "GLOBAL_DB_SPLIT_MISMATCH",
            "cells.expect_global_db is true but database.services.identity is "
            "absent; control-service refuses to start in exactly this state",
            where,
        ))
    elif not cfg.expect_global_db and cfg.identity_database_target is not None:
        findings.append(Finding(
            "error", "GLOBAL_DB_GUARD_DISABLED",
            "database.services.identity is set without cells.expect_global_db, "
            "which disables the startup guard that makes the split safe: a later "
            "typo in the override would silently put identity tables in the cell "
            "database and every cell would share one cell's users",
            where,
        ))

    if (
        # Only meaningful for a CELL profile. The global tier's primary database
        # IS the identity database — that is its definition, not a defect.
        cfg.profile not in IDENTITY_ONLY_PROFILES
        and cfg.identity_database_target
        and cfg.identity_database_target == cfg.database_target
    ):
        findings.append(Finding(
            "error", "IDENTITY_IN_CELL_DATABASE",
            f"identity resolves to the same database as the cell "
            f"({cfg.database_target}); one cell's failure would then be a "
            "platform-wide login outage",
            where,
        ))
    return findings


def check_egress_honesty(cfg: ResolvedConfig) -> list[Finding]:
    """An egress proxy is only meaningful where a NetworkPolicy enforces it.

    Only the Kubernetes profiles ship those policies. Elsewhere a configured
    proxy mediates SOME sandbox traffic while the rest flows freely, which reads
    as "egress is controlled" and is not true on the wire.
    """
    if cfg.egress_proxy_url and not cfg.profile.startswith("k8s-"):
        return [Finding(
            "error", "EGRESS_PROXY_WITHOUT_ENFORCEMENT",
            f"services.egress_proxy is set to {cfg.egress_proxy_url!r} but this "
            "profile ships no NetworkPolicy, so the proxy would be one option "
            "among many rather than a sandbox's only outbound path",
            f"conf/platform.{cfg.profile}.yaml",
        )]
    return []


def _secret_present(var: str) -> bool:
    """Is *var* supplied by the current environment (or a truth file)?"""
    if os.environ.get(var):
        return True
    truth_file = TRUTH_FILES.get(var)
    return truth_file is not None and truth_file.is_file()


def check_secrets(profile: str) -> list[Finding]:
    """Report which secrets the CURRENT environment supplies.

    Opt-in (``--check-secrets``): the answer describes the shell this runs in,
    which is meaningful before a release or inside a deployment pipeline, and
    meaningless when merely inspecting a profile.
    """
    findings: list[Finding] = []
    for spec in SECRETS:
        if _secret_present(spec.var):
            continue
        # The global tier runs identity alone: runtime/control tokens are not
        # its concern, and demanding them would train operators to ignore this.
        if profile in IDENTITY_ONLY_PROFILES and spec.var in {
            "WORKSPACE_RUNTIME_INTERNAL_TOKEN",
            "CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN",
            "AGENT_SERVICE_LLM_API_KEY",
        }:
            continue
        findings.append(Finding(
            "error" if spec.required else "warning",
            "SECRET_MISSING",
            f"{spec.var} is not set (needed by {spec.consumers}). "
            f"Without it: {spec.without_it}",
            f"env:{spec.var}",
        ))
    return findings


def check_port_conflicts() -> list[Finding]:
    """Two services must never claim one port."""
    seen: dict[int, str] = {}
    findings: list[Finding] = []
    for service, port in sorted(PLATFORM_PORTS.items(), key=lambda kv: kv[1]):
        if port in seen:
            findings.append(Finding(
                "error", "PORT_CONFLICT",
                f"{service} and {seen[port]} both claim port {port}",
                "scripts/config-doctor.py:PLATFORM_PORTS",
            ))
        seen[port] = service
    return findings


# ── Rendering ─────────────────────────────────────────────────────────────


def render(cfg: ResolvedConfig) -> str:
    lines = [
        f"{CYAN}profile {cfg.profile}{RESET}",
        f"  config files      {' + '.join(cfg.config_files)}",
        f"  database          {cfg.database_url}",
    ]
    if cfg.identity_database_target:
        lines.append(f"  identity database {cfg.identity_database_target}  (global tier)")
    lines += [
        f"  expect_global_db  {cfg.expect_global_db}",
        f"  redis             {cfg.redis_url}",
        f"  workspace base    {cfg.workspace_base_path or '(unset)'}",
        f"  log level         {cfg.log_level}",
        "  service topology",
    ]
    for name, url in cfg.service_urls.items():
        lines.append(f"    {name:12s} {url or DIM + '(unset)' + RESET}")
    if cfg.egress_proxy_url:
        lines.append(f"  egress proxy      {cfg.egress_proxy_url}")
    return "\n".join(lines)


def render_secret_table() -> str:
    lines = [f"{CYAN}secrets in the current environment{RESET}"]
    for spec in SECRETS:
        present = _secret_present(spec.var)
        mark = f"{GREEN}set{RESET}" if present else (
            f"{RED}MISSING{RESET}" if spec.required else f"{YELLOW}unset{RESET}"
        )
        need = "required" if spec.required else "optional"
        source = ""
        if not os.environ.get(spec.var):
            truth_file = TRUTH_FILES.get(spec.var)
            if truth_file is not None and truth_file.is_file():
                source = " (truth file)"
        lines.append(
            f"  {spec.var:42s} {mark:20s} {DIM}{need} — {spec.consumers}{source}{RESET}"
        )
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────


def run(profiles: Iterable[str], *, show: bool, want_secrets: bool) -> Report:
    report = Report()
    report.findings += check_profile_files()
    report.findings += check_base_is_address_free()
    report.findings += check_port_conflicts()

    for profile in profiles:
        if not (CONF_DIR / f"platform.{profile}.yaml").exists():
            continue  # already reported by check_profile_files
        cfg = resolve(profile)
        report.configs.append(cfg)
        report.findings += check_addresses(cfg)
        report.findings += check_global_cell_split(cfg)
        report.findings += check_egress_honesty(cfg)
        if want_secrets:
            report.findings += check_secrets(profile)

        if show:
            print(render(cfg))
            print()

    if want_secrets:
        print(render_secret_table())
        print()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile", action="append", dest="profiles", metavar="NAME",
        help=f"profile to inspect ({'|'.join(PROFILES)}); repeatable, "
             "default: all of them",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="print the effective configuration, not just the findings "
             "(implied when --profile is given)",
    )
    parser.add_argument(
        "--check-secrets", action="store_true",
        help="also report which secrets the current environment supplies",
    )
    args = parser.parse_args()

    # The shared package holds the loader this tool must not re-implement.
    sys.path.insert(0, str(SHARED_PYTHON))
    try:
        import datacode_common.config.platform  # noqa: F401
    except ImportError as exc:
        print(
            f"{RED}ERROR{RESET} cannot import datacode_common ({exc}).\n"
            f"      Install the shared package: pip install -e shared/python",
            file=sys.stderr,
        )
        return 1

    # The loader warns about a missing profile on stderr. Here that warning is
    # noise: this tool sets the profile deliberately, once per profile.
    import logging

    logging.getLogger("datacode_common.config.platform").setLevel(logging.ERROR)

    profiles = args.profiles or list(PROFILES)
    unknown = [p for p in profiles if p not in PROFILES]
    if unknown:
        print(
            f"{RED}ERROR{RESET} unknown profile(s): {', '.join(unknown)}\n"
            f"      known profiles: {' '.join(PROFILES)}",
            file=sys.stderr,
        )
        return 1

    print()
    report = run(
        profiles,
        show=args.show or bool(args.profiles),
        want_secrets=args.check_secrets,
    )

    if report.findings:
        print(f"{CYAN}findings{RESET}")
        for finding in report.findings:
            print(finding.render())
        print()

    errors = len(report.errors)
    warnings = len(report.findings) - errors
    checked = ", ".join(c.profile for c in report.configs)
    if errors:
        print(f"{RED}FAIL{RESET} {errors} error(s), {warnings} warning(s) "
              f"across [{checked}]")
        return 1
    if warnings:
        print(f"{YELLOW}OK{RESET}   0 errors, {warnings} warning(s) "
              f"across [{checked}]")
        return 0
    print(f"{GREEN}OK{RESET}   configuration is consistent across [{checked}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
