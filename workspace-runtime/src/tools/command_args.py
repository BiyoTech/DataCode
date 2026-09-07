"""CLI argument parsing for the governed verbs (design 04 §3.6).

Moved here from the shell shims, where the same flags were parsed with
``for arg in "$@"`` loops that tracked the previous token by hand. Two things
improve by construction rather than by care:

* a flag expecting a value and not getting one is an ERROR here, where the shim
  silently consumed the next flag as its value (``--start --end 2024-01-01``
  used to set ``start=--end``);
* unknown flags are reported instead of ignored, so a typo does not become a
  plan that quietly used defaults.

Parsers are built with ``add_help=False`` and never write to stdout/stderr:
this runs inside a request, so a parse failure must become a tool result, not a
process exit. ``argparse.ArgumentError`` is raised and translated by the caller.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


class CommandArgumentError(ValueError):
    """A governed command's arguments could not be parsed."""


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises instead of exiting the process."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CommandArgumentError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        if message:
            raise CommandArgumentError(message.strip())
        raise CommandArgumentError("")


@dataclass
class PlanArgs:
    """Parsed ``sqlmesh plan`` / ``sqlmesh apply`` arguments.

    ``environment`` is what the CALLER TYPED, kept only so a disagreement with
    the authoritative environment can be reported. It is never used as a target:
    the working copy is per-environment, and honouring a typed name would let a
    hallucinated argument deploy somewhere else.
    """

    environment: str = ""
    gateway: str = ""
    start: str = ""
    end: str = ""
    skip_tests: bool = False
    skip_backfill: bool = False
    no_gaps: bool = False
    forward_only: bool = False
    no_auto_categorization: bool = False
    include_unmodified: bool = False
    auto_apply: bool = False
    restate_models: list[str] = field(default_factory=list)

    def plan_dates(self) -> Optional[dict[str, str]]:
        """The date window, or ``None`` when the caller named neither bound."""
        dates = {}
        if self.start:
            dates["start"] = self.start
        if self.end:
            dates["end"] = self.end
        return dates or None

    def plan_options(self) -> dict[str, Any]:
        """Options in the shape ``PlanService`` expects."""
        options: dict[str, Any] = {
            "skip_tests": self.skip_tests,
            "skip_backfill": self.skip_backfill,
            "no_gaps": self.no_gaps,
            "forward_only": self.forward_only,
            "no_auto_categorization": self.no_auto_categorization,
            "include_unmodified": self.include_unmodified,
        }
        if self.restate_models:
            options["restate_models"] = list(self.restate_models)
        return options


def parse_plan_args(argv: Sequence[str]) -> PlanArgs:
    """Parse the arguments of ``sqlmesh plan`` / ``sqlmesh apply``.

    Accepts the native binary's flags so an agent (or a user) writing the command
    they already know does not have to learn a platform dialect.
    """
    parser = _Parser(prog="sqlmesh plan", add_help=False)
    parser.add_argument("environment", nargs="?", default="")
    parser.add_argument("--gateway", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--skip-tests", action="store_true", dest="skip_tests")
    parser.add_argument("--skip-backfill", action="store_true", dest="skip_backfill")
    parser.add_argument("--no-gaps", action="store_true", dest="no_gaps")
    parser.add_argument("--forward-only", action="store_true", dest="forward_only")
    parser.add_argument(
        "--no-auto-categorization",
        action="store_true",
        dest="no_auto_categorization",
    )
    parser.add_argument(
        "--include-unmodified", action="store_true", dest="include_unmodified"
    )
    parser.add_argument("--auto-apply", action="store_true", dest="auto_apply")
    # Repeatable, matching the native CLI: `--restate-model a --restate-model b`.
    parser.add_argument(
        "--restate-model", action="append", default=[], dest="restate_models"
    )

    namespace = parser.parse_args(list(argv))
    return PlanArgs(
        environment=namespace.environment or "",
        gateway=namespace.gateway or "",
        start=namespace.start or "",
        end=namespace.end or "",
        skip_tests=namespace.skip_tests,
        skip_backfill=namespace.skip_backfill,
        no_gaps=namespace.no_gaps,
        forward_only=namespace.forward_only,
        no_auto_categorization=namespace.no_auto_categorization,
        include_unmodified=namespace.include_unmodified,
        auto_apply=namespace.auto_apply,
        restate_models=list(namespace.restate_models or []),
    )


@dataclass
class ExternalModelsArgs:
    """Parsed ``sqlmesh create_external_models`` arguments."""

    schema: str = ""
    gateway: str = ""
    include: str = ""
    exclude: str = ""
    samples: int = 3
    strict: bool = False


def parse_external_models_args(argv: Sequence[str]) -> ExternalModelsArgs:
    """Parse ``sqlmesh create_external_models`` arguments."""
    parser = _Parser(prog="sqlmesh create_external_models", add_help=False)
    parser.add_argument("--schema", default="")
    parser.add_argument("--gateway", default="")
    parser.add_argument("--include", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--strict", action="store_true")

    namespace = parser.parse_args(list(argv))
    return ExternalModelsArgs(
        schema=namespace.schema or "",
        gateway=namespace.gateway or "",
        include=namespace.include or "",
        exclude=namespace.exclude or "",
        samples=namespace.samples,
        strict=namespace.strict,
    )


@dataclass
class JobRunArgs:
    """Parsed ``dc run`` arguments."""

    job_name: str = ""
    config_json: str = ""


def parse_job_run_args(argv: Sequence[str]) -> JobRunArgs:
    """Parse ``dc run <job_name> [--config-json J]``."""
    parser = _Parser(prog="dc run", add_help=False)
    parser.add_argument("job_name", nargs="?", default="")
    parser.add_argument("--config-json", default="", dest="config_json")

    namespace = parser.parse_args(list(argv))
    return JobRunArgs(
        job_name=namespace.job_name or "",
        config_json=namespace.config_json or "",
    )


def has_flag(argv: Sequence[str], flag: str) -> bool:
    """Whether *flag* appears in *argv*. For the presence-only switches."""
    return flag in tuple(argv)


__all__ = [
    "CommandArgumentError",
    "ExternalModelsArgs",
    "JobRunArgs",
    "PlanArgs",
    "has_flag",
    "parse_external_models_args",
    "parse_job_run_args",
    "parse_plan_args",
]
