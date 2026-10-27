"""Git failure reason-code contract.

A reason code has four consumers that must agree: the runtime's classifier, the
runtime's structured error, the control plane's relay, and the frontend's i18n
map. Only the first three can share a Python import; the frontend keeps its own
copy, so it is asserted here — the same approach ``test_git_providers.py`` uses
for the provider table and ``lease-vectors.json`` uses for the lease algebra.

The defect this pins down: a code emitted by the producer but absent from the
frontend map silently degrades to the generic "Git operation failed", which is
exactly the message users complained about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from datacode_common.git_reasons import (
    GIT_REMOTE_REASONS,
    REASON_MESSAGES,
    GitRemoteReason,
    message_for,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTIFY_ERROR = REPO_ROOT / "frontend" / "src" / "utils" / "notifyError.ts"
EN_LOCALE = REPO_ROOT / "frontend" / "src" / "i18n" / "locales" / "en.ts"
ZH_LOCALE = REPO_ROOT / "frontend" / "src" / "i18n" / "locales" / "zh-CN.ts"


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_every_declared_reason_is_listed_and_has_a_message() -> None:
    """A code without a message renders as the unknown template — a silent loss."""
    declared = {
        value
        for name, value in vars(GitRemoteReason).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    assert declared == set(GIT_REMOTE_REASONS)
    assert set(REASON_MESSAGES) == set(GIT_REMOTE_REASONS)


def test_messages_only_use_supported_placeholders() -> None:
    """``message_for`` supplies host and operation; anything else would KeyError."""
    for reason, template in REASON_MESSAGES.items():
        placeholders = set(re.findall(r"\{(\w+)\}", template))
        assert placeholders <= {"host", "operation"}, reason
        # Formatting must not raise for any reason code.
        assert message_for(reason, host="example.com", operation="fetch")


def test_unresolved_host_reads_as_a_sentence_not_a_gap() -> None:
    rendered = message_for(GitRemoteReason.REPO_NOT_FOUND)
    assert "the Git server" in rendered
    assert "{host}" not in rendered


def test_unknown_reason_falls_back_to_the_unknown_template() -> None:
    rendered = message_for("something_new", host="git.example.com", operation="fetch")
    assert rendered == REASON_MESSAGES[GitRemoteReason.UNKNOWN].format(
        host="git.example.com", operation="fetch"
    )


def test_repo_creation_denied_is_distinct_from_no_write_access() -> None:
    """Different fixes: create-in-org vs contents:write on an existing repo."""
    assert GitRemoteReason.REPO_CREATION_DENIED != GitRemoteReason.NO_WRITE_ACCESS
    denied = REASON_MESSAGES[GitRemoteReason.REPO_CREATION_DENIED]
    assert "create" in denied.lower()


# ---------------------------------------------------------------------------
# Cross-language agreement
# ---------------------------------------------------------------------------


def _frontend_reason_keys() -> dict[str, str]:
    """Parse GIT_REASON_KEYS out of the frontend error parser."""
    source = NOTIFY_ERROR.read_text(encoding="utf-8")
    block = re.search(
        r"const GIT_REASON_KEYS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\n\}",
        source,
        re.DOTALL,
    )
    assert block, "GIT_REASON_KEYS not found in notifyError.ts"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def test_frontend_maps_exactly_the_backend_reasons() -> None:
    assert set(_frontend_reason_keys()) == set(GIT_REMOTE_REASONS)


@pytest.mark.parametrize("locale", [EN_LOCALE, ZH_LOCALE])
def test_every_frontend_reason_key_has_a_translation(locale: Path) -> None:
    """A missing key renders the raw i18n path to the user."""
    source = locale.read_text(encoding="utf-8")
    git_block = re.search(r"\n    git: \{(.*?)\n    \},", source, re.DOTALL)
    assert git_block, f"errors.git block not found in {locale.name}"
    translated = set(re.findall(r"(\w+):", git_block.group(1)))
    missing = sorted(
        key.rsplit(".", 1)[1]
        for key in _frontend_reason_keys().values()
        if key.rsplit(".", 1)[1] not in translated
    )
    assert not missing, f"{locale.name} lacks errors.git translations for: {missing}"


def test_single_source_of_truth_no_service_local_copy() -> None:
    """Structural guard: a second copy is how a contract literal drifts."""
    offenders: list[str] = []
    pattern = re.compile(
        r"^(class GitRemoteReason|_?GIT_REASON_MESSAGES\s*[:=])", re.M
    )
    for service in ("control-service", "workspace-runtime", "agent-service"):
        for path in sorted((REPO_ROOT / service / "src").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(
                    f"{service}/{path.relative_to(REPO_ROOT / service)}:{line}"
                )
    assert not offenders, (
        "import from datacode_common.git_reasons: " + "; ".join(offenders)
    )
