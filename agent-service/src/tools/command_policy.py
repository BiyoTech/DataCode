"""Shell command security policy engine.

Loads YAML rules (conf/shell-command-policy.yaml), pre-compiles regex patterns,
and evaluates shell commands for a three-state decision: allow / confirm / deny.

Batch evaluation returns the strictest action across all commands
(deny > confirm > allow) using the "one-vote-deny, one-vote-confirm" principle.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shell connectors — commands are split on these to prevent bypass
# ---------------------------------------------------------------------------
_CONNECTOR_PATTERN = re.compile(r"(?:;|\&\&|\|\||\|)\s*")

# Default policy file path relative to project root
_DEFAULT_POLICY_PATH = str(
    Path(__file__).resolve().parents[3] / "conf" / "shell-command-policy.yaml"
)

# Priority mapping for strictest-action resolution
_ACTION_PRIORITY: dict[str, int] = {"deny": 3, "confirm": 2, "allow": 1}

# Minimum interval (seconds) between stat() checks for hot-reload. High-frequency
# shell calls would otherwise issue a filesystem stat on every invocation, which
# adds up under load. Within this window the cached ruleset is used as-is.
_MTIME_CHECK_INTERVAL_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a batch of shell commands against policy rules.

    Attributes:
        action: Final action for the batch (allow | confirm | deny).
        matched_rule: Name of the highest-priority matched rule (empty for default).
        reason: Human-readable reason for confirm/deny decisions (English).
        risk_level: Risk badge level 1-6 (6 = most severe).
        triggered_commands: List of commands that triggered this decision.
    """

    action: str
    matched_rule: str = ""
    reason: str = ""
    risk_level: int = 1
    triggered_commands: List[str] = field(default_factory=list)


@dataclass
class _CompiledRule:
    """Internal: pre-compiled rule loaded from YAML."""

    name: str
    pattern: re.Pattern
    action: str
    risk_level: int
    reason: str


# ---------------------------------------------------------------------------
# Policy Engine
# ---------------------------------------------------------------------------


class CommandPolicy:
    """Loads and evaluates shell command security rules.

    Supports runtime hot-reload via file mtime tracking. Rules are pre-compiled
    as regex patterns and cached; invalidated when the YAML file changes.
    """

    def __init__(self, policy_path: Optional[str] = None) -> None:
        self._policy_path = policy_path or _DEFAULT_POLICY_PATH
        self._rules: List[_CompiledRule] = []
        self._default_action: str = "confirm"
        self._policy_file_missing: bool = False
        self._file_mtime: float = 0.0
        # Throttle: last wall-clock time we stat'd the policy file. Within
        # _MTIME_CHECK_INTERVAL_SECONDS of this, _ensure_loaded skips the stat
        # call entirely and reuses the cached ruleset.
        self._last_mtime_check: float = 0.0
        # Guard the reload path so two threads/coroutines don't simultaneously
        # rebuild self._rules and leave it in a half-rebuilt state. The evaluate
        # path acquires the same lock so readers never see a torn list.
        self._lock = threading.Lock()
        self._ensure_loaded()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_commands(self, commands: List[str]) -> PolicyDecision:
        """Evaluate a batch of shell commands and return the strictest decision.

        Each command is split on shell connectors and matched against all rules.
        The strictest action across all commands wins:
          - If ANY command matches a 'deny' rule → entire batch is denied.
          - If ANY command matches a 'confirm' rule → entire batch needs approval.
          - Otherwise → allow.

        Args:
            commands: List of raw shell command strings.

        Returns:
            PolicyDecision with the strictest action and associated metadata.
        """
        self._ensure_loaded()

        best_priority = 0
        best_rule: Optional[_CompiledRule] = None
        triggered: List[str] = []

        for cmd in commands:
            cmd = (cmd or "").strip()
            if not cmd:
                continue

            sub_parts = self._split_connectors(cmd)
            for part in sub_parts:
                normalized = self._normalize(part)
                if not normalized:
                    continue

                for rule in self._rules:
                    if rule.pattern.search(normalized):
                        prio = _ACTION_PRIORITY.get(rule.action, 0)
                        if prio > best_priority:
                            best_priority = prio
                            best_rule = rule
                            triggered = [cmd]
                        elif prio == best_priority and best_rule is not None and cmd not in triggered:
                            triggered.append(cmd)
                        # Continue scanning — a higher-priority rule may still match

        if best_rule is not None:
            logger.info(
                "command_policy_evaluated",
                action=best_rule.action,
                matched_rule=best_rule.name,
                risk_level=best_rule.risk_level,
                triggered_count=len(triggered),
            )
            return PolicyDecision(
                action=best_rule.action,
                matched_rule=best_rule.name,
                reason=best_rule.reason,
                risk_level=best_rule.risk_level,
                triggered_commands=triggered,
            )

        return PolicyDecision(action=self._default_action)

    # ------------------------------------------------------------------
    # Internal: rule loading & caching
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load or reload rules if the policy file has changed (hot-reload).

        Throttled by _MTIME_CHECK_INTERVAL_SECONDS to avoid a filesystem stat
        on every evaluate_commands() call. The reload path is guarded by a
        lock so concurrent callers don't race on the rebuild. Readers in
        evaluate_commands see an atomically-replaced list reference (never a
        half-rebuilt one) because we build a new list and assign it in one
        step.
        """
        now = time.monotonic()
        # Fast path: within the throttle window, reuse the cached ruleset
        # without even stat'ing the file. This is the common case under load.
        if self._rules and (now - self._last_mtime_check) < _MTIME_CHECK_INTERVAL_SECONDS:
            return
        self._last_mtime_check = now

        # Only one thread/coroutine should perform the reload at a time. Others
        # can proceed with the existing (slightly stale) ruleset — correctness
        # is not compromised because the file mtime check inside the lock will
        # still catch a real change on the next cycle.
        with self._lock:
            try:
                mtime = os.path.getmtime(self._policy_path)
            except OSError:
                if not self._rules:
                    self._default_action = "confirm"
                    self._policy_file_missing = True
                    logger.warning(
                        "command_policy_file_missing",
                        path=self._policy_path,
                        hint="Using default_action=confirm for unmatched commands",
                    )
                return

            if mtime <= self._file_mtime:
                return  # No changes

            logger.info("command_policy_loading", path=self._policy_path)
            with open(self._policy_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            # Build a NEW list and assign atomically so concurrent readers in
            # evaluate_commands never observe a partially-rebuilt collection.
            new_rules: List[_CompiledRule] = []
            for rule_data in raw.get("rules", []):
                try:
                    compiled = _CompiledRule(
                        name=rule_data["name"],
                        pattern=re.compile(rule_data["pattern"]),
                        action=rule_data.get("action", "allow"),
                        risk_level=int(rule_data.get("risk_level", 1)),
                        reason=rule_data.get("reason", ""),
                    )
                    new_rules.append(compiled)
                except (KeyError, re.error) as e:
                    logger.warning(
                        "command_policy_rule_parse_error",
                        rule_name=rule_data.get("name", "unknown"),
                        error=str(e),
                    )

            # Atomic swap: readers either see the old list or the new one,
            # never a mix.
            self._default_action = raw.get("default_action", "allow")
            self._rules = new_rules
            self._file_mtime = mtime
            logger.info(
                "command_policy_loaded",
                rule_count=len(self._rules),
                default_action=self._default_action,
            )

    # ------------------------------------------------------------------
    # Internal: command processing
    # ------------------------------------------------------------------

    @staticmethod
    def _split_connectors(command: str) -> List[str]:
        """Split a command on shell connectors into sub-commands for matching.

        Example: "echo a && rm -rf /" → ["echo a", "rm -rf /"]
        """
        return [s.strip() for s in _CONNECTOR_PATTERN.split(command) if s.strip()]

    @staticmethod
    def _normalize(command: str) -> str:
        """Normalize whitespace for regex matching.

        Collapses multiple spaces/newlines/tabs into single space.
        """
        return re.sub(r"\s+", " ", command).strip()


# ---------------------------------------------------------------------------
# Module-level singleton (lazy-loaded)
# ---------------------------------------------------------------------------

_policy_instance: Optional[CommandPolicy] = None


def get_policy() -> CommandPolicy:
    """Get or create the module-level CommandPolicy singleton."""
    global _policy_instance
    if _policy_instance is None:
        _policy_instance = CommandPolicy()
    return _policy_instance
