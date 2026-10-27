"""Which shell verbs the PLATFORM owns, and how to recognise them.

A handful of CLI verbs are not really shell commands: ``sqlmesh apply`` needs an
environment lock, an audit record and a writable warehouse account that user
code must never coexist with (Q5). Those verbs are served by the governance
plane inside workspace-runtime — the same code path the Web buttons use — and
never by the sandbox.

This module is the SINGLE declaration of that set, because three independent
places have to agree on it:

* ``workspace-runtime/src/tools/command_router.py`` — dispatches a governed verb
  to the governance plane instead of the sandbox;
* ``task-runner/src/guards/`` — refuses a governed verb that reached a sandbox
  anyway (a compound command routes to the sandbox by design, see below), so the
  native binary cannot be used to walk around the governance plane;
* ``agent-service/src/tools/command_policy.py`` — splits on the same connectors
  when it evaluates the approval policy.

Two lists that disagree would produce exactly the bug this set exists to
prevent: a verb the router does not claim and the guard does not refuse runs
natively, unlocked and unaudited.

Simple commands only
--------------------
Routing looks at the FIRST verb of a command. That is only sound for a single
simple command: ``sqlmesh plan dev && rm -rf models`` would be routed on its
first two words and the rest would be silently dropped. So a command carrying
any shell metacharacter is NOT routed — it goes to the sandbox, which is what
the sandbox is for. The guard scripts are what keep that from being a
governance bypass.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional, Sequence

# ── verbs the platform owns ───────────────────────────────────────────────
#
# Key: the leading argv tokens that identify the verb, matched longest-first.
# Value: what to tell a caller that reached the native binary with it — used
# verbatim by the sandbox guards, so the message a user sees does not depend on
# which layer caught the command.
#
# `sqlmesh run` is listed even though the platform REFUSES it: without an entry
# here the native binary would happily refresh data outside the environment
# guardrail, the lock and the audit trail.
GOVERNED_VERBS: dict[tuple[str, ...], str] = {
    ("sqlmesh", "plan"): (
        "'sqlmesh plan' is served by the platform (environment lock, state "
        "fingerprint, audit). Run it as a single command without shell "
        "operators so the platform can serve it."
    ),
    ("sqlmesh", "apply"): (
        "'sqlmesh apply' is served by the platform (environment lock, apply "
        "history, writable warehouse account). Run it as a single command "
        "without shell operators so the platform can serve it."
    ),
    ("sqlmesh", "run"): (
        "'sqlmesh run' is disabled on this platform: it refreshes data outside "
        "the environment guardrail, the environment lock and the audit trail. "
        "Use 'sqlmesh plan' / 'sqlmesh apply' for versioned data landing, or "
        "'dc run <job_name>' for an orchestrated refresh."
    ),
    ("sqlmesh", "create_external_models"): (
        "'sqlmesh create_external_models' is served by the platform (it writes "
        "into the working copy through the schema-discovery service). Run it as "
        "a single command without shell operators."
    ),
    ("sqlmesh", "ontology"): (
        "'sqlmesh ontology' is served by the platform's ontology API. Run it as "
        "a single command without shell operators."
    ),
    ("dc", "assets"): (
        "'dc assets' is served by the platform's Dagster data plane. Run it as "
        "a single command without shell operators."
    ),
    ("dc", "deployment"): (
        "'dc deployment' is served by the platform's Dagster data plane. Run it "
        "as a single command without shell operators."
    ),
    ("dc", "run"): (
        "'dc run' is served by the platform's Dagster data plane (the run is "
        "stamped with the deployment's tags). Run it as a single command "
        "without shell operators."
    ),
    ("dc", "deps"): (
        "'dc deps' is served by the platform: the install runs in a sandbox that "
        "may reach the package index and nothing else, and its result is recorded "
        "so the working copy's dependency tree stays explainable. Run it as a "
        "single command without shell operators."
    ),
    # Listed even though the platform REFUSES it, for the same reason as
    # `sqlmesh run`: without an entry here the native binary would install into
    # whichever interpreter happens to be on PATH. In a sandbox that is a tree
    # thrown away when the vehicle exits (so the packages vanish and the failure
    # is "it worked once"), and in the code location's own interpreter it would
    # be a shared mutation no declaration records.
    ("pip", "install"): (
        "'pip install' is disabled on this platform: a package installed by hand "
        "is gone with the sandbox that ran it, and nothing records why it was "
        "there. Declare the dependency in asset-dag/pyproject.toml under "
        "[project].dependencies, then run 'dc deps sync' (or press Reload in the "
        "UI) to install it for the whole working copy."
    ),
    ("pip3", "install"): (
        "'pip3 install' is disabled on this platform: declare the dependency in "
        "asset-dag/pyproject.toml under [project].dependencies and run "
        "'dc deps sync' (or press Reload in the UI) instead."
    ),
}

# Programs that carry governed verbs at all. Used by the guards to decide
# whether they need to be on PATH, and by tests asserting the two stay aligned.
GOVERNED_PROGRAMS: frozenset[str] = frozenset(verb[0] for verb in GOVERNED_VERBS)

# ── shell shape detection ─────────────────────────────────────────────────

# Connectors the approval policy splits on, so a rule matching `rm -rf /`
# cannot be bypassed by hiding it behind `echo ok && `.
CONNECTOR_PATTERN = re.compile(r"(?:;|\&\&|\|\||\|)\s*")

# Anything here means the string is more than one simple command, or that its
# arguments are not knowable without running a shell. Both disqualify routing.
#
#   ; & | < >   composition and redirection
#   ` $ ( )     substitution and expansion — the argv we would parse is not the
#               argv bash would build
#   newline     several commands in one string
SHELL_METACHARACTERS: frozenset[str] = frozenset(";&|<>`$()\n\r")


def split_connectors(command: str) -> list[str]:
    """Split a command into sub-commands on shell connectors.

    Example: ``echo a && rm -rf /`` -> ``["echo a", "rm -rf /"]``.
    """
    return [part.strip() for part in CONNECTOR_PATTERN.split(command) if part.strip()]


def is_simple_command(command: str) -> bool:
    """Whether *command* is one simple command with literal arguments.

    Glob characters are deliberately NOT disqualifying: the governed verbs that
    take patterns (``--include``, ``--exclude``) pass them to an API that does
    its own matching, so receiving them unexpanded is correct.
    """
    return not any(char in SHELL_METACHARACTERS for char in command)


def match_governed_verb(argv: Sequence[str]) -> Optional[tuple[str, ...]]:
    """Return the governed verb *argv* starts with, or ``None``.

    Matched longest-first so a future ``("dc", "deployment", "reload")`` entry
    would win over ``("dc", "deployment")`` instead of being shadowed by it.
    The program name is compared by BASENAME: ``./sqlmesh apply`` and
    ``/usr/local/bin/sqlmesh apply`` are the same verb.
    """
    if not argv:
        return None
    tokens = (os.path.basename(argv[0]), *argv[1:])
    for verb in sorted(GOVERNED_VERBS, key=len, reverse=True):
        if len(tokens) >= len(verb) and tuple(tokens[: len(verb)]) == verb:
            return verb
    return None


def governance_hint(verb: Iterable[str]) -> str:
    """The message describing why *verb* belongs to the platform."""
    return GOVERNED_VERBS.get(tuple(verb), "This command is served by the platform.")


__all__ = [
    "CONNECTOR_PATTERN",
    "GOVERNED_PROGRAMS",
    "GOVERNED_VERBS",
    "SHELL_METACHARACTERS",
    "governance_hint",
    "is_simple_command",
    "match_governed_verb",
    "split_connectors",
]
