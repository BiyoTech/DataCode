"""The governed-verb declaration and the guards generated from it.

Two invariants that no single service can assert on its own:

1. the router's dispatch set and the guards' refusal set come from ONE
   declaration — a verb claimed by neither runs natively, unlocked and
   unaudited;
2. routing only ever applies to a single simple command, because argv derived
   from a compound string is a guess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from datacode_common.command_guards import (
    GUARD_DIR_NAME,
    render_guard_script,
    write_guards,
)
from datacode_common.command_verbs import (
    GOVERNED_PROGRAMS,
    GOVERNED_VERBS,
    governance_hint,
    is_simple_command,
    match_governed_verb,
    split_connectors,
)


class TestGovernedVerbMatching:
    def test_an_ordinary_command_is_not_governed(self):
        assert match_governed_verb(["ls", "-la"]) is None
        assert match_governed_verb(["pytest", "-q"]) is None
        assert match_governed_verb(["git", "status"]) is None

    def test_plan_and_apply_are_governed(self):
        assert match_governed_verb(["sqlmesh", "plan", "dev"]) == ("sqlmesh", "plan")
        assert match_governed_verb(["sqlmesh", "apply", "prod"]) == ("sqlmesh", "apply")

    def test_verbs_that_execute_user_code_are_not_governed(self):
        """test / audit / dag run the user's own Python — that is what L3 is for.

        Governing them would move user code execution into the platform process,
        which is the opposite of the isolation the sandbox exists to provide.
        """
        for verb in ("test", "audit", "dag", "render", "evaluate", "fetchdf"):
            assert match_governed_verb(["sqlmesh", verb]) is None, verb

    def test_the_program_is_matched_by_basename(self):
        """An absolute or relative path names the same program."""
        assert match_governed_verb(["/usr/local/bin/sqlmesh", "apply"]) == (
            "sqlmesh",
            "apply",
        )
        assert match_governed_verb(["./sqlmesh", "apply"]) == ("sqlmesh", "apply")

    def test_dc_falls_through_to_the_desk_calculator(self):
        """`dc` is also POSIX dc; unknown subcommands must stay its own."""
        assert match_governed_verb(["dc", "-e", "2 2 + p"]) is None

    def test_empty_argv_is_not_governed(self):
        assert match_governed_verb([]) is None

    def test_every_verb_has_a_hint(self):
        """The guards print these verbatim, so an empty one would be a bare
        refusal the user cannot act on."""
        for verb in GOVERNED_VERBS:
            assert governance_hint(verb).strip()

    def test_governed_programs_is_derived_not_restated(self):
        assert GOVERNED_PROGRAMS == frozenset(verb[0] for verb in GOVERNED_VERBS)


class TestSimpleCommandDetection:
    """The routing precondition. A false positive here is a dropped command."""

    def test_a_plain_command_is_simple(self):
        assert is_simple_command("sqlmesh plan dev")
        assert is_simple_command("sqlmesh apply prod --skip-tests")

    @pytest.mark.parametrize(
        "command",
        [
            "sqlmesh plan dev && rm -rf models",
            "sqlmesh apply prod; whoami",
            "sqlmesh plan dev || true",
            "sqlmesh plan dev | tee out.txt",
            "sqlmesh plan dev > out.txt",
            "sqlmesh plan dev < in.txt",
            "sqlmesh plan `whoami`",
            "sqlmesh plan $(whoami)",
            "sqlmesh plan $ENV",
            "sqlmesh plan dev &",
            "sqlmesh plan dev\nsqlmesh apply prod",
        ],
    )
    def test_anything_composed_or_expanded_is_not_simple(self, command):
        """Routing on the first verb would silently drop the rest, or would parse
        an argv bash would not have built."""
        assert not is_simple_command(command)

    def test_glob_patterns_stay_simple(self):
        """The governed verbs taking patterns pass them to an API that matches
        them itself, so receiving them unexpanded is correct."""
        assert is_simple_command("sqlmesh create_external_models --include 'raw_*'")

    def test_connector_split_matches_what_the_policy_evaluates(self):
        assert split_connectors("echo a && rm -rf /") == ["echo a", "rm -rf /"]
        assert split_connectors("  ;; ") == []


class TestGeneratedGuards:
    """The guards are the ONLY thing standing between a compound command
    containing a governed verb and the native binary."""

    def test_a_guard_exists_for_every_governed_program(self, tmp_path):
        directory = Path(write_guards(tmp_path / GUARD_DIR_NAME))
        for program in GOVERNED_PROGRAMS:
            script = directory / program
            assert script.is_file()
            assert script.stat().st_mode & 0o111, "must be executable"

    def test_the_script_carries_every_verb_of_its_program(self):
        script = render_guard_script("sqlmesh")
        for verb in GOVERNED_VERBS:
            if verb[0] != "sqlmesh":
                continue
            assert verb[1] in script, verb
        # And nothing belonging to the other program.
        assert "assets" not in script

    def test_writing_is_idempotent(self, tmp_path):
        first = write_guards(tmp_path / GUARD_DIR_NAME)
        content = (Path(first) / "sqlmesh").read_text(encoding="utf-8")
        second = write_guards(tmp_path / GUARD_DIR_NAME)
        assert first == second
        assert (Path(second) / "sqlmesh").read_text(encoding="utf-8") == content


@pytest.mark.skipif(sys.platform == "win32", reason="bash guards are POSIX")
class TestGuardBehaviourEndToEnd:
    """Real bash, real PATH, a stub standing in for the native binary.

    Asserting on the generated TEXT would not catch a script that refuses
    everything, or one that recurses into itself.
    """

    @pytest.fixture
    def sandbox(self, tmp_path):
        guard_dir = Path(write_guards(tmp_path / GUARD_DIR_NAME))
        real = tmp_path / "real"
        real.mkdir()
        for program in GOVERNED_PROGRAMS:
            stub = real / program
            stub.write_text(
                f'#!/bin/sh\necho "NATIVE {program}: $*"\n', encoding="utf-8"
            )
            stub.chmod(0o755)
        # Guards FIRST, the native stubs last: the same order the sandbox PATH
        # uses, and the reason the guard wins the lookup.
        path = f"{guard_dir}:/usr/bin:/bin:{real}"
        return {"PATH": path, "HOME": str(tmp_path)}

    def _run(self, sandbox, command: str):
        # `bash -c`, matching task-runner's own invocation: a LOGIN shell would
        # source /etc/profile, which overwrites PATH and would discard the guard
        # directory entirely. See task-runner/src/shell.py::_SHELL_ARGV.
        return subprocess.run(
            ["bash", "-c", command],
            env=sandbox,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @pytest.mark.parametrize(
        "command",
        [
            "sqlmesh apply prod",
            "sqlmesh plan dev",
            "sqlmesh run",
            "sqlmesh ontology list",
            "dc run nightly",
            "dc assets",
            "dc deployment status",
            "dc deps sync",
            "pip install polars",
            "pip3 install polars",
        ],
    )
    def test_a_governed_verb_is_refused(self, sandbox, command):
        result = self._run(sandbox, command)
        assert result.returncode != 0, result.stdout
        assert "[datacode]" in result.stderr
        # The native binary must not have run: reaching it IS the bypass.
        assert "NATIVE" not in result.stdout

    def test_the_bypass_this_exists_for_is_closed(self, sandbox):
        """The case routing cannot handle: a governed verb inside a compound
        command. It reaches the sandbox by design, and must die here."""
        result = self._run(sandbox, "echo start && sqlmesh apply prod && echo done")
        assert "start" in result.stdout
        assert "NATIVE" not in result.stdout
        assert "done" not in result.stdout
        assert "[datacode]" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "sqlmesh test models/x",
            "sqlmesh audit",
            "sqlmesh dag",
            "sqlmesh --version",
            # Only `install` is governed: reading what IS installed is how a user
            # or an agent diagnoses a dependency problem, and refusing it would
            # remove the tool needed to understand the refusal.
            "pip list",
            "pip show polars",
        ],
    )
    def test_everything_else_reaches_the_real_binary(self, sandbox, command):
        result = self._run(sandbox, command)
        assert result.returncode == 0, result.stderr
        assert "NATIVE" in result.stdout

    def test_the_pip_refusal_names_the_declaration_and_the_command(self, sandbox):
        """A refusal that does not say what to do instead is just an obstacle."""
        result = self._run(sandbox, "pip install polars")
        assert "pyproject.toml" in result.stderr
        assert "dc deps sync" in result.stderr

    def test_the_desk_calculator_still_works(self, sandbox):
        """`dc` is also POSIX dc, and the guard must not shadow it.

        Asserted on the RESULT rather than on reaching the stub: the system's own
        /usr/bin/dc comes earlier on PATH here, and either answer proves the same
        thing — the guard forwarded instead of refusing.
        """
        result = self._run(sandbox, "dc -e '2 2 + p'")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() in ("4", "NATIVE dc: -e 2 2 + p")

    def test_the_guard_does_not_recurse_into_itself(self, sandbox):
        """It resolves the real binary with its OWN directory stripped from PATH.

        Without that the passthrough would find the guard again and loop until
        the process table gave up.
        """
        result = self._run(sandbox, "sqlmesh test")
        assert result.stdout.count("NATIVE sqlmesh") == 1

    def test_a_missing_native_binary_is_reported_not_hung(self, tmp_path):
        """No real binary on PATH: exit 127, the shell's own "not found" code."""
        guard_dir = Path(write_guards(tmp_path / GUARD_DIR_NAME))
        env = {"PATH": f"{guard_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
        result = subprocess.run(
            ["bash", "-c", "sqlmesh test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 127
        assert "not installed in this sandbox" in result.stderr

    def test_a_login_shell_would_discard_the_guards(self, sandbox):
        """The reason task-runner uses `bash -c` and not `bash -lc`.

        A login shell sources /etc/profile, which rewrites PATH from scratch on
        Debian (and reorders it through path_helper on macOS). The guard
        directory does not survive, so the governed verb reaches the native
        binary — the exact bypass the guards exist to close.

        Kept as an executable record of the defect: if this ever stops holding,
        the comment in shell.py should be revisited rather than trusted.
        """
        login = subprocess.run(
            ["bash", "-lc", "echo $PATH"],
            env=sandbox,
            capture_output=True,
            text=True,
            timeout=30,
        )
        non_login = subprocess.run(
            ["bash", "-c", "echo $PATH"],
            env=sandbox,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert GUARD_DIR_NAME in non_login.stdout
        # Not asserted as "absent from login": the behaviour differs per distro.
        # What matters is that the non-login form is the one that keeps it.
        assert non_login.stdout.split(":")[0].endswith(GUARD_DIR_NAME)
        del login
