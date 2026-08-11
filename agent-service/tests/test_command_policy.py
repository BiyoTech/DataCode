"""Tests for shell command policy fail-closed behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.command_policy import CommandPolicy


def test_missing_policy_file_uses_confirm_default(tmp_path: Path) -> None:
    missing = tmp_path / "missing-policy.yaml"
    policy = CommandPolicy(policy_path=str(missing))
    decision = policy.evaluate_commands(["echo hello"])
    assert decision.action == "confirm"
    assert policy._policy_file_missing is True


def test_default_action_from_yaml(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "default_action: deny\nrules: []\n",
        encoding="utf-8",
    )
    policy = CommandPolicy(policy_path=str(policy_file))
    decision = policy.evaluate_commands(["echo hello"])
    assert decision.action == "deny"


def test_existing_rules_still_apply(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """
default_action: confirm
rules:
  - name: allow_echo
    pattern: "^echo "
    action: allow
    risk_level: 1
    reason: safe
""".strip(),
        encoding="utf-8",
    )
    policy = CommandPolicy(policy_path=str(policy_file))
    decision = policy.evaluate_commands(["echo hello"])
    assert decision.action == "allow"
    assert decision.matched_rule == "allow_echo"
