"""The build-time assertion that opt-in image extras actually landed.

Its whole reason for existing is that ``pip install "./svc[typo]"`` exits 0 with
a warning. The one job these extras have is putting a package into the platform
baseline, and the failure they must not have is a silent one: an image built
with a malformed ``RUNTIME_EXTRAS`` looks healthy, carries none of the packages,
and only reveals itself much later as a ``deps_no_wheel`` refusal for the exact
package the build was meant to provide.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from datacode_common.image_extras import main, parse_extras, verify_extras


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        textwrap.dedent(
            """
            [project]
            name = "svc"

            [project.optional-dependencies]
            spark = ["pyspark>=3.5.0"]
            # A package nothing installs, to exercise the second check.
            absent = ["a-package-that-is-not-installed>=1.0"]
            # Present under every interpreter running these tests.
            present = ["pytest>=7.0"]
            """
        ),
        encoding="utf-8",
    )
    return path


class TestParsing:
    def test_commas_separate_names(self) -> None:
        assert parse_extras("spark,k8s") == ["spark", "k8s"]

    def test_whitespace_separates_too(self) -> None:
        """A pasted command line must come apart into the names it contains.

        Treating it as one exotic name would report a single unknown extra of
        gibberish; splitting it names `make`, `build` and `up`, which is what
        actually went wrong.
        """
        assert parse_extras("spark make build up") == ["spark", "make", "build", "up"]

    def test_empty_is_empty(self) -> None:
        assert parse_extras("") == []
        assert parse_extras("   ") == []
        assert parse_extras(",,") == []


class TestAnUnknownExtraIsRejected:
    def test_a_typo_is_named(self, pyproject: Path) -> None:
        problems = verify_extras(pyproject, "RUNTIME_EXTRAS", "sparkk")
        assert len(problems) == 1
        assert "sparkk" in problems[0]
        # The known groups are listed, because the operator has to pick one.
        assert "spark" in problems[0]

    def test_a_pasted_command_line_is_rejected_word_by_word(
        self, pyproject: Path
    ) -> None:
        """The accident this module was written for.

        `ARG RUNTIME_EXTRAS="spark make build up"` — the shell command copied
        into the Dockerfile default. pip read four extra names, recognised one,
        warned about three, and installed nothing extra.
        """
        problems = verify_extras(pyproject, "RUNTIME_EXTRAS", "spark make build up")
        assert {"make", "build", "up"} == {
            word for word in ("make", "build", "up") for p in problems if word in p
        }
        assert len(problems) == 3, (
            "the valid `spark` name must not be reported, and each invalid one "
            "must be"
        )


class TestAGroupThatDidNotInstallIsRejected:
    def test_a_declared_but_absent_package_is_caught(self, pyproject: Path) -> None:
        problems = verify_extras(pyproject, "RUNTIME_EXTRAS", "absent")
        assert len(problems) == 1
        assert "a-package-that-is-not-installed" in problems[0]
        assert "did not apply" in problems[0]

    def test_a_group_whose_packages_are_present_passes(self, pyproject: Path) -> None:
        assert verify_extras(pyproject, "RUNTIME_EXTRAS", "present") == []


class TestTheOrdinaryBuild:
    def test_no_extras_is_success(self, pyproject: Path) -> None:
        """Extras are opt-in: asking for none is the common case, not an error."""
        assert verify_extras(pyproject, "RUNTIME_EXTRAS", "") == []

    def test_the_entry_point_exits_zero(self, pyproject: Path) -> None:
        assert main([str(pyproject), "RUNTIME_EXTRAS", ""]) == 0


class TestTheEntryPoint:
    def test_a_bad_value_exits_one(self, pyproject: Path) -> None:
        assert main([str(pyproject), "RUNTIME_EXTRAS", "nope"]) == 1

    def test_a_missing_pyproject_exits_two(self, tmp_path: Path) -> None:
        """Distinguished from a bad value: this one is a broken build context,
        not an operator's typo."""
        assert main([str(tmp_path / "nope.toml"), "RUNTIME_EXTRAS", "spark"]) == 2

    def test_wrong_arity_exits_two(self, pyproject: Path) -> None:
        assert main([str(pyproject)]) == 2


class TestTheRealImagesUseIt:
    """The check is worth nothing if the Dockerfiles do not run it."""

    @pytest.mark.parametrize(
        ("dockerfile", "arg"),
        [
            ("workspace-runtime/Dockerfile", "RUNTIME_EXTRAS"),
            ("task-runner/Dockerfile", "RUNNER_EXTRAS"),
        ],
    )
    def test_the_image_verifies_its_own_build_arg(
        self, dockerfile: str, arg: str
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        source = (root / dockerfile).read_text(encoding="utf-8")
        assert "datacode_common.image_extras" in source, (
            f"{dockerfile} installs {arg} without verifying it landed"
        )
        assert f'"${{{arg}}}"' in source, (
            f"{dockerfile} runs the check but does not pass {arg} to it"
        )
