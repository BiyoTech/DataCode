"""The command router: which plane serves a shell command, and how it renders.

The security-relevant assertions are about ROUTING, not about the governed
operations themselves (those have their own tests). Three things must hold:

* an ordinary command is never routed away from the sandbox;
* a governed verb is never sent to the sandbox as a single simple command;
* a COMPOUND command is always sent to the sandbox, even when it contains a
  governed verb — argv derived from it would be a guess, and the sandbox's
  generated guards are what refuse it there.
"""

from __future__ import annotations

import pytest

from src.tools import command_render as render
from src.tools.command_args import (
    CommandArgumentError,
    parse_external_models_args,
    parse_job_run_args,
    parse_plan_args,
)
from src.tools.command_router import classify


class TestClassification:
    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "grep -rn TODO models/",
            "pytest -q",
            "git status",
            "python -c 'print(1)'",
            # Executes the user's own Python and test code: exactly what L3 is
            # for, so it must NOT be pulled into the platform process.
            "sqlmesh test",
            "sqlmesh audit",
            "sqlmesh dag",
            "sqlmesh render my_model",
        ],
    )
    def test_ordinary_commands_go_to_the_sandbox(self, command):
        assert classify(command) is None

    @pytest.mark.parametrize(
        ("command", "verb"),
        [
            ("sqlmesh plan dev", ("sqlmesh", "plan")),
            ("sqlmesh apply prod --skip-tests", ("sqlmesh", "apply")),
            ("sqlmesh run", ("sqlmesh", "run")),
            (
                "sqlmesh create_external_models --schema raw",
                ("sqlmesh", "create_external_models"),
            ),
            ("sqlmesh ontology list", ("sqlmesh", "ontology")),
            ("dc assets --json", ("dc", "assets")),
            ("dc deployment status", ("dc", "deployment")),
            ("dc run nightly", ("dc", "run")),
        ],
    )
    def test_governed_verbs_are_routed(self, command, verb):
        routed = classify(command)
        assert routed is not None
        assert routed.verb == verb

    @pytest.mark.parametrize(
        "command",
        [
            "sqlmesh plan dev && rm -rf models",
            "sqlmesh apply prod; whoami",
            "sqlmesh apply prod | tee log",
            "sqlmesh plan $(cat env.txt)",
            "sqlmesh apply prod > out.txt",
        ],
    )
    def test_a_compound_command_is_never_routed(self, command):
        """Routing on the first verb would silently drop the rest.

        These reach the sandbox, where the generated guards refuse the governed
        verb — see shared/python/tests/test_command_verbs.py.
        """
        assert classify(command) is None

    def test_unbalanced_quoting_is_not_routed(self):
        """bash would parse this differently from shlex, so any argv is a guess."""
        assert classify("sqlmesh apply 'prod") is None

    def test_blank_input_is_not_routed(self):
        assert classify("") is None
        assert classify("   ") is None

    def test_leading_whitespace_does_not_hide_a_governed_verb(self):
        routed = classify("   sqlmesh apply prod  ")
        assert routed is not None
        assert routed.verb == ("sqlmesh", "apply")


class TestPlanArguments:
    def test_the_native_flags_are_accepted(self):
        args = parse_plan_args(
            [
                "dev",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-31",
                "--skip-tests",
                "--forward-only",
                "--restate-model",
                "a",
                "--restate-model",
                "b",
            ]
        )
        assert args.environment == "dev"
        assert args.plan_dates() == {"start": "2024-01-01", "end": "2024-01-31"}
        options = args.plan_options()
        assert options["skip_tests"] is True
        assert options["forward_only"] is True
        assert options["restate_models"] == ["a", "b"]

    def test_no_dates_means_no_window(self):
        assert parse_plan_args(["dev"]).plan_dates() is None

    def test_a_flag_expecting_a_value_and_not_getting_one_is_an_error(self):
        """The shim's hand-rolled loop set start="--end" here and planned on it."""
        with pytest.raises(CommandArgumentError):
            parse_plan_args(["dev", "--start", "--end", "2024-01-01"]).plan_dates()

    def test_an_unknown_flag_is_reported_not_ignored(self):
        with pytest.raises(CommandArgumentError):
            parse_plan_args(["dev", "--no-such-flag"])

    def test_restate_models_is_absent_when_unused(self):
        """An empty list would read as "restate nothing explicitly", which is a
        different request from not restating."""
        assert "restate_models" not in parse_plan_args(["dev"]).plan_options()

    def test_external_models_requires_nothing_at_parse_time(self):
        """The missing --schema is a USAGE error with a usage message, produced by
        the handler; the parser must not turn it into a traceback."""
        args = parse_external_models_args([])
        assert args.schema == ""
        assert args.samples == 3

    def test_external_models_flags(self):
        args = parse_external_models_args(
            ["--schema", "raw", "--gateway", "wh", "--samples", "5", "--strict"]
        )
        assert (args.schema, args.gateway, args.samples, args.strict) == (
            "raw",
            "wh",
            5,
            True,
        )

    def test_job_run_arguments(self):
        args = parse_job_run_args(["nightly", "--config-json", '{"a":1}'])
        assert args.job_name == "nightly"
        assert args.config_json == '{"a":1}'


class TestRendering:
    """The text an agent reads. Carried over from the shims verbatim in
    structure, because prompts and skills were written against it."""

    def test_no_changes_is_stated_plainly(self):
        assert render.plan_preview({"has_changes": False}) == "No changes to apply."

    def test_a_plan_lists_its_changes(self):
        text = render.plan_preview(
            {
                "has_changes": True,
                "changes": {
                    "added": ["a"],
                    "removed": [],
                    "modified": {
                        "direct": [
                            {"view_name": "db.orders", "change_category": "breaking"}
                        ],
                        "indirect": [{"name": "db.summary"}],
                        "metadata": [],
                    },
                },
                "requires_backfill": True,
                "backfills": [
                    {"view_name": "db.orders", "batches": 3, "interval": ["d1", "d2"]}
                ],
                "state_fingerprint": "fp1",
            }
        )
        assert "Plan Summary:" in text
        assert "Added: 1, Removed: 0" in text
        assert "db.orders [breaking]" in text
        assert "db.summary" in text
        assert "db.orders: 3 batch(es)" in text
        assert "State Fingerprint: fp1" in text

    def test_an_applied_plan_exits_zero(self):
        text, code = render.plan_apply(
            {
                "applied": True,
                "changes": {"added": ["a"], "removed": [], "modified": {}},
                "backfills": [{"name": "db.orders", "batches": [1, 2]}],
            }
        )
        assert code == 0
        assert "applied successfully" in text
        assert "db.orders: 2 batch(es)" in text

    def test_failing_tests_exit_four(self):
        """A distinct code because it is a distinct action: fix the tests."""
        text, code = render.plan_apply(
            {
                "applied": False,
                "tests_failed": True,
                "test_results": {"failures": ["test_a"]},
            }
        )
        assert code == 4
        assert "tests failed" in text
        assert "test_a" in text

    def test_nothing_to_apply_exits_zero(self):
        text, code = render.plan_apply({"applied": False, "has_changes": False})
        assert code == 0
        assert "nothing to apply" in text

    def test_a_failed_apply_exits_five(self):
        text, code = render.plan_apply(
            {"applied": False, "has_changes": True, "reason": "lock_held"}
        )
        assert code == 5
        assert "lock_held" in text

    def test_a_disagreeing_environment_is_reported_not_honoured(self):
        """The working copy is per-environment, so the argument cannot take
        effect. Saying so is what makes a hallucinated target visible."""
        notice = render.environment_notice("prod", "dev")
        assert "prod" in notice and "dev" in notice

    def test_an_agreeing_environment_is_silent(self):
        assert render.environment_notice("dev", "dev") == ""
        assert render.environment_notice("", "dev") == ""

    def test_the_result_shape_matches_the_sandbox(self):
        """Nothing downstream may be able to tell which plane served a command."""
        result = render.result(0, stdout="out", stderr="err")
        assert set(result) == {"exit_code", "stdout", "stderr", "timed_out"}
        assert result["timed_out"] is False

    def test_output_is_bounded(self):
        result = render.result(0, stdout="x" * (render.MAX_OUTPUT_CHARS + 100))
        assert len(result["stdout"]) == render.MAX_OUTPUT_CHARS

    def test_an_empty_ontology_list_says_so(self):
        assert render.ontology_list([]) == "No ontologies found in workspace."

    def test_the_asset_graph_renders_dependencies(self):
        text = render.asset_graph(
            {
                "nodes": [{"name": "orders", "group_name": "raw", "deps": ["src"]}],
                "edges": [{"source": "src", "target": "orders"}],
            },
            as_json=False,
        )
        assert "1 assets, 1 dependencies" in text
        assert "[raw] orders  <- src" in text
