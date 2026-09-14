"""The packages user code may import are a promise, and promises get enforced.

A Dagster code location imports the user's ``defs/`` package on THIS service's
interpreter — locations deliberately share one interpreter instead of getting a
per-workspace venv (see ``CodeLocationManager._spawn_grpc``). Whatever user code
can import is therefore a property of this image, not of the user's project: the
``pyproject.toml`` the platform scaffolds into every working copy is metadata
that nothing ever installs (it exists to tell Dagster the ``module_name``).

That makes an undeclared package a latent outage. pandas, numpy and pyarrow were
in the image only as transitive dependencies of sqlmesh and daft; this file never
mentioned them, so an upstream bump could have dropped any of them without a
single test noticing. The failure mode is also quieter than most: an import at
module level stops the location loading, but an import inside an asset body lets
the location report healthy and the asset show up in the DAG, and only
materializing it raises ``ModuleNotFoundError``.

So the check is bidirectional, because a promise recorded in two places will
drift:

* every name the ``usercode`` extra declares must resolve on this interpreter;
* the set declared there must be exactly the set this test expects, so adding a
  package to one and not the other turns red instead of going unnoticed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "workspace-runtime" / "pyproject.toml"
DOCKERFILE = REPO_ROOT / "workspace-runtime" / "Dockerfile"

# Distribution name -> importable module. Only the mismatches belong here:
# several data packages install under a name nothing imports (`scikit-learn`
# gives you `sklearn`, `getdaft` gives you `daft`), and getting this wrong
# makes a package that is perfectly installed look absent.
IMPORT_NAMES = {"scikit-learn": "sklearn", "getdaft": "daft"}

# The platform's promise to user code, mirrored from the `usercode` extra.
# Kept as data rather than read out of pyproject.toml: the point of the reverse
# assertion is that TWO independent statements of the promise must agree. If
# this list were derived from the file, it could never disagree with it.
EXPECTED_USERCODE = {"pandas", "numpy", "pyarrow", "scikit-learn", "scipy"}


def _module_of(distribution: str) -> str:
    return IMPORT_NAMES.get(distribution, distribution)


def _install_commands(source: str) -> list[str]:
    """Every ``pip install`` invocation in ``source``, comments stripped.

    A substring search over the file is NOT good enough, and fails in the worst
    direction: the comments explaining WHY the extra is installed contain the
    very string being looked for, so the check keeps passing after somebody
    removes the extra from the command — which is the only regression it exists
    to catch. Comments and line continuations are stripped, then the commands
    are recovered from what remains.
    """
    lines = [
        line.split("#", 1)[0].rstrip("\\").strip() for line in source.splitlines()
    ]
    joined = " ".join(line for line in lines if line)
    return [match.group(0) for match in re.finditer(r"pip install[^\n&|;]*", joined)]


def _declared_usercode() -> set[str]:
    """Distribution names declared by the ``usercode`` extra."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - the project requires >= 3.11
        raise AssertionError("tomllib needs Python 3.11+")

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "usercode" in extras, (
        "pyproject.toml has no `usercode` extra: the packages user code may "
        "import are then undeclared, and a dependency bump upstream can remove "
        "them without any test noticing"
    )
    return {
        requirement.split(">=")[0].split("==")[0].split("<")[0].strip()
        for requirement in extras["usercode"]
    }


class TestThePromiseIsKept:
    @pytest.mark.parametrize(
        "distribution", sorted(EXPECTED_USERCODE), ids=sorted(EXPECTED_USERCODE)
    )
    def test_it_is_importable_here(self, distribution: str) -> None:
        module = _module_of(distribution)
        assert importlib.util.find_spec(module) is not None, (
            f"`{distribution}` is declared by the `usercode` extra but is not "
            f"importable on this interpreter. Code locations run here, so an "
            f"asset importing it fails — at load time if the import is at module "
            f"level, or only on materialization if it sits inside the asset body."
        )

    def test_the_declared_set_matches_what_is_expected(self) -> None:
        """Bidirectional: the extra and this list must agree, in both directions."""
        declared = _declared_usercode()
        assert declared == set(EXPECTED_USERCODE), (
            f"the `usercode` extra declares {sorted(declared)} but this test "
            f"expects {sorted(EXPECTED_USERCODE)}. A promise written in two "
            f"places only holds while they agree — update both."
        )


class TestThePromiseIsInstalledWhereTheCodeRuns:
    """Declaring is not installing. These three are where a code location
    actually executes, and all of them must carry the extra."""

    def test_the_image_installs_it(self) -> None:
        commands = _install_commands(DOCKERFILE.read_text(encoding="utf-8"))
        assert any("workspace-runtime[usercode]" in c for c in commands), (
            "the runtime image installs the service without `[usercode]`, so "
            "the packages this service promises user code are absent exactly "
            "where production code locations load"
        )

    def test_the_dev_venv_installs_it(self) -> None:
        source = (REPO_ROOT / "scripts" / "dev-lib.sh").read_text(encoding="utf-8")
        # The extras reach the install as an ARGUMENT, so the call site is what
        # has to name them — the install line itself only interpolates $extras.
        calls = [
            line.split("#", 1)[0].strip()
            for line in source.splitlines()
            if "ensure_python_venv workspace-runtime" in line.split("#", 1)[0]
        ]
        assert calls, "dev-lib.sh no longer sets up workspace-runtime's venv"
        assert any('"dev,usercode"' in call for call in calls), (
            "dev-lib.sh installs workspace-runtime without the `usercode` "
            "extra, so a fresh dev venv is an environment where ordinary assets "
            "fail to import while production works"
        )

    def test_ci_installs_it(self) -> None:
        source = (
            REPO_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        commands = _install_commands(source)
        assert any("[dev,usercode]" in c for c in commands), (
            "CI installs workspace-runtime without `[usercode]`, which would "
            "make this module — the one place meant to catch a missing package "
            "— unable to see it"
        )


class TestTheScaffoldTellsUsersTheTruth:
    """The generated README is where a user learns what they may import. A
    table that lists a package the image does not carry — or omits one it does
    — sends them straight into a ModuleNotFoundError with no way to see why."""

    def test_every_promised_package_is_documented(self) -> None:
        readme = (
            REPO_ROOT / "workspace-runtime" / "src" / "templates" / "README.md.tpl"
        ).read_text(encoding="utf-8")
        for distribution in sorted(EXPECTED_USERCODE):
            assert _module_of(distribution) in readme, (
                f"the scaffolded README never mentions "
                f"`{_module_of(distribution)}`, so a user is told nothing about "
                f"a package this service promises code locations"
            )

    def test_the_documented_packages_are_all_installed(self) -> None:
        """The reverse: nothing gets advertised that is not actually there."""
        readme = (
            REPO_ROOT / "workspace-runtime" / "src" / "templates" / "README.md.tpl"
        ).read_text(encoding="utf-8")
        table = next(
            block
            for block in readme.split("## ")
            if block.startswith("Available packages")
        )
        # Rows are header, then separator, then the packages: the header cell
        # is prose ("Module you import"), not something anybody can import.
        rows = [row for row in table.splitlines() if row.startswith("|")][2:]
        listed = {
            name.strip(" `")
            for row in rows
            for name in row.split("|")[1].split(",")
            if name.strip(" `")
        }
        assert listed, "no packages parsed out of the README table"
        missing = {
            name
            for name in listed
            if importlib.util.find_spec(_module_of(name)) is None
        }
        assert not missing, (
            f"the scaffolded README advertises {sorted(missing)}, which is not "
            f"importable on this interpreter"
        )

    def test_the_scaffold_project_says_how_to_install(self) -> None:
        """The generated ``pyproject.toml`` IS the install list now.

        It used to be metadata only, and the file said so — which was the honest
        thing to write while nothing installed it. Now that the platform does, the
        file has to say the opposite, and say WHERE the install is triggered: a
        user who adds a package and sees nothing happen has no way to guess that a
        Reload is what applies it.
        """
        template = (
            REPO_ROOT / "workspace-runtime" / "src" / "templates" / "pyproject.toml.tpl"
        ).read_text(encoding="utf-8")

        assert "METADATA ONLY" not in template, (
            "the scaffolded pyproject.toml still claims nothing installs it, "
            "which stopped being true when `deps.sync` landed"
        )
        assert "INSTALL LIST" in template
        # The two ways to apply it, named in the file itself.
        assert "dc deps sync" in template
        assert "Reload" in template


class TestScaffoldAndUserCodeAgree:
    """What a generated project declares must be importable here.

    The scaffolded declaration names platform-provided packages, and a code
    location imports them on THIS interpreter. A name in that list that this
    service cannot import would produce a working copy whose very first reload
    fails on a line the user never wrote.
    """

    def test_the_scaffold_project_dependencies_are_kept(self) -> None:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover - the project requires >= 3.11
            raise AssertionError("tomllib needs Python 3.11+")

        # Rendered through the real substitution rather than by replacing one
        # placeholder here: the dependency block and the Python floor are now
        # GENERATED from the platform baseline, so a hand-rolled replacement
        # would parse a template that no working copy ever receives.
        from src.codeloc.scaffold import render_placeholders

        template = (
            REPO_ROOT / "workspace-runtime" / "src" / "templates" / "pyproject.toml.tpl"
        )
        data = tomllib.loads(
            render_placeholders(template.read_text(encoding="utf-8"), "ws")
        )
        requirements = data["project"]["dependencies"]
        assert requirements, "the scaffold must document what it was written against"
        for requirement in requirements:
            name = requirement.split(">=")[0].split("==")[0].strip()
            assert importlib.util.find_spec(_module_of(name)) is not None, (
                f"the scaffolded asset-dag declares `{name}`, which is not "
                f"importable here. The declaration is generated from this "
                f"service's own baseline, so a miss means the generator and the "
                f"environment disagree."
            )

    def test_the_scaffold_declares_the_running_python_floor(self) -> None:
        """A hardcoded floor drifts from the image it is supposed to describe."""
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover - the project requires >= 3.11
            raise AssertionError("tomllib needs Python 3.11+")

        from datacode_common.python_deps import interpreter_python_version

        from src.codeloc.scaffold import render_placeholders

        template = (
            REPO_ROOT / "workspace-runtime" / "src" / "templates" / "pyproject.toml.tpl"
        )
        data = tomllib.loads(
            render_placeholders(template.read_text(encoding="utf-8"), "ws")
        )
        assert data["project"]["requires-python"] == (
            f">={interpreter_python_version()}"
        )
