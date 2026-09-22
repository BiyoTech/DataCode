"""Failure codes: one string, one meaning, from the installer to the UI.

This file exists because of a specific defect. A declaration that conflicted
with the platform baseline was refused with `ERR_DEPS_DECLARATION`, and the
frontend renders that code as "the dependency declaration could not be read".
The user's file was valid TOML, so they went looking for a syntax error that was
never there — and the real cause, a version bound the platform cannot satisfy,
was never named.

Nothing about that was visible in a test, because each layer was tested against
its own copy of the strings. So the assertions here are deliberately
cross-layer: the shared declaration, the runner's raised code, the reload job
envelope and BOTH translation catalogues, checked against each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from datacode_common import python_deps

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_LOCALES = REPO_ROOT / "frontend" / "src" / "i18n" / "locales"

# Every dependency failure the platform can report. The list is asserted to be
# complete against the shared module, so adding a code without adding it here
# fails rather than silently skipping the checks below.
DEPS_FAILURE_CODES = (
    "deps_declaration_invalid",
    "deps_platform_conflict",
    # A declared package whose own dependencies fight the baseline. Its own code
    # because the next action is different: another release of what the user
    # declared, or an operator — never "accept the platform's version of it".
    "deps_platform_transitive",
    "deps_install_failed",
    # The index HAS a matching version, it just publishes no usable wheel. Its own
    # code because every other failure asks the user to edit a version bound, and
    # this is the one where the bound is already correct.
    "deps_no_wheel",
    "deps_index_not_configured",
    "deps_shadow_platform",
    "deps_too_large",
)


def test_the_shared_module_declares_exactly_these_codes():
    declared = {
        value
        for name, value in vars(python_deps).items()
        if name.startswith("FAILURE_") and isinstance(value, str)
    }
    assert declared == set(DEPS_FAILURE_CODES)


def test_a_platform_conflict_is_not_reported_as_an_unreadable_file():
    """THE regression. Distinct codes because the next action differs.

    "Fix the syntax" and "accept the platform's version" are different
    instructions, and collapsing them sent users to look for a problem their
    file did not have.
    """
    assert (
        python_deps.FAILURE_PLATFORM_CONFLICT
        != python_deps.FAILURE_DECLARATION_INVALID
    )


def test_the_runner_raises_the_conflict_code_for_a_conflict():
    """Checked at the boundary the code actually crosses.

    The runner runs in another image and reports through the task protocol, so a
    mismatch here is invisible until a real install refuses a real declaration.
    """
    source = (REPO_ROOT / "task-runner" / "src" / "deps_ops.py").read_text(
        encoding="utf-8"
    )
    # The handler for the conflict exception must raise the conflict code.
    handler = re.search(
        r"except PlatformCoreConflict as exc:(.*?)(?=\n    except |\n    [a-z#]|\Z)",
        source,
        re.DOTALL,
    )
    assert handler, "deps_ops no longer handles PlatformCoreConflict"
    body = handler.group(1)
    assert "ERR_DEPS_PLATFORM_CONFLICT" in body
    assert "ERR_DEPS_DECLARATION" not in body, (
        "a platform conflict is being reported with the declaration code again; "
        "the UI renders that as 'the file could not be read'"
    )


def test_the_reload_envelope_reuses_the_shared_codes():
    """Re-typing them in the job module is how the drift happened."""
    from src.codeloc import reload_jobs

    assert reload_jobs.FAILURE_DEPS_DECLARATION == python_deps.FAILURE_DECLARATION_INVALID
    assert reload_jobs.FAILURE_DEPS_PLATFORM == python_deps.FAILURE_PLATFORM_CONFLICT
    assert reload_jobs.FAILURE_DEPS_INSTALL == python_deps.FAILURE_INSTALL_FAILED


@pytest.mark.parametrize("locale", ["en.ts", "zh-CN.ts"])
@pytest.mark.parametrize("code", DEPS_FAILURE_CODES)
def test_every_code_has_a_message_in_every_locale(locale, code):
    """An unknown code falls back to "failed unexpectedly", which says nothing.

    Both catalogues, because a code added to English only leaves half the users
    with a message that names no action.
    """
    text = (FRONTEND_LOCALES / locale).read_text(encoding="utf-8")
    assert f"{code}:" in text, f"{code} has no message in {locale}"


@pytest.mark.parametrize("locale", ["en.ts", "zh-CN.ts"])
def test_the_conflict_message_does_not_talk_about_reading_the_file(locale):
    """The wording is the whole point of the separate code.

    A conflict message that says "could not be read" would reproduce the
    original defect with a different code attached.
    """
    text = (FRONTEND_LOCALES / locale).read_text(encoding="utf-8")
    start = text.index("deps_platform_conflict:")
    message = text[start : text.index("deps_install_failed:", start)]
    forbidden = ("could not be parsed", "could not be read", "无法解析", "无法读取")
    assert not any(phrase in message for phrase in forbidden), (
        f"the platform-conflict message in {locale} still describes a parse "
        f"failure: {message.strip()}"
    )
