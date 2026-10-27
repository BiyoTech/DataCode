"""The sandbox image has the same "no wheel" escape hatch as the runtime one.

A cluster deployment runs user code HERE: the Kubernetes task backend submits
every task to this image, and `dagster.code_location_mode: pod` loads a working
copy's `defs/` package in it. So a package PyPI ships without a wheel — pyspark,
one ~300 MB source distribution per release — can only reach user code by being
baked into this image, exactly as it reaches a dev code location through the
workspace-runtime image.

Two properties are worth pinning, because both fail quietly:

* the build-arg is APPENDED and never substituted, so a plain build installs
  nothing extra and `RUNNER_EXTRAS=spark` adds to the ordinary install;
* asking for Spark also installs a JVM. pyspark bundles the jars but not a
  runtime to execute them, so the missing half surfaces as `JAVA_HOME is not
  set` on the first action, long after the image was declared healthy.

And one that is easy to get wrong from the outside: the platform baseline is
computed by workspace-runtime off ITS OWN interpreter (see
`workspace-runtime/src/deps/baseline.py`), while the import happens here. A
deployment that bakes Spark into only one of the two gets the worst pairing —
the install refuses a package the sandbox could have imported — so the build
script has to carry both arguments.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "task-runner" / "Dockerfile"
PYPROJECT = REPO_ROOT / "task-runner" / "pyproject.toml"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-images.sh"


def _install_commands(source: str) -> list[str]:
    """Every `pip install` invocation in *source*, comments stripped.

    Stripping comments is what makes the check meaningful: the prose explaining
    the build-arg contains the very string being looked for, so a substring
    search keeps passing after the interpolation is dropped from the command.
    """
    lines = [
        line.split("#", 1)[0].rstrip("\\").strip() for line in source.splitlines()
    ]
    joined = " ".join(line for line in lines if line)
    return [match.group(0) for match in re.finditer(r"pip install[^\n&|;]*", joined)]


class TestTheBuildArgIsWired:
    def test_it_is_declared_and_appended(self) -> None:
        source = DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG RUNNER_EXTRAS" in source, (
            "the task-runner image has no extras build-arg, so a package with "
            "no wheel cannot reach user code in a cluster deployment"
        )
        commands = _install_commands(source)
        assert any("./task-runner${RUNNER_EXTRAS:+" in c for c in commands), (
            "RUNNER_EXTRAS is declared but never reaches the install, so "
            "`--build-arg RUNNER_EXTRAS=spark` builds an unchanged image and "
            "says nothing"
        )

    def test_the_default_build_installs_no_extra(self) -> None:
        """The brackets must appear ONLY when the arg is set.

        `./task-runner[]` is not a valid requirement specifier; an unconditional
        one breaks every build, including the ones that never ask for anything.
        """
        commands = _install_commands(DOCKERFILE.read_text(encoding="utf-8"))
        assert not any("./task-runner[]" in c for c in commands), (
            "the install emits an empty extras group when RUNNER_EXTRAS is unset"
        )

    def test_the_default_is_empty(self) -> None:
        """`ARG RUNNER_EXTRAS=` must default to nothing.

        Extras are opt-in per deployment, and this arg is meant to be supplied
        from the OUTSIDE (`--build-arg`, which compose forwards from the
        environment). Baking a value into the Dockerfile inverts that: every
        build pays for it. It has also been a real accident — a shell command
        line (`RUNTIME_EXTRAS=spark make build up`) pasted into the default,
        which pip then read as four extra NAMES, warned about, and skipped.
        """
        default = re.search(
            r"^ARG RUNNER_EXTRAS=(.*)$", DOCKERFILE.read_text(encoding="utf-8"), re.M
        )
        assert default is not None, "ARG RUNNER_EXTRAS is gone"
        assert default.group(1).strip().strip('"').strip("'") == "", (
            "ARG RUNNER_EXTRAS has a baked-in default "
            f"({default.group(1)!r}); pass it with --build-arg instead"
        )

    def test_a_bad_value_fails_the_build(self) -> None:
        """pip only WARNS about an unknown extra, so the build must check.

        Without this step a typo, or a whole command line pasted into the
        build-arg, produces an image that installs none of the packages, exits
        0, and only reveals itself much later as a `deps_no_wheel` refusal for
        the exact package the build was supposed to provide.
        """
        source = DOCKERFILE.read_text(encoding="utf-8")
        assert "datacode_common.image_extras" in source, (
            "the task-runner image does not verify RUNNER_EXTRAS landed"
        )


class TestAskingForSparkGetsAWholeRuntime:
    def test_the_extra_exists(self) -> None:
        import tomllib

        extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]
        assert any("pyspark" in item for item in extras.get("spark", ())), (
            "task-runner declares no `spark` extra, so RUNNER_EXTRAS=spark "
            "names a group that does not exist"
        )

    def test_a_jvm_comes_with_it(self) -> None:
        source = DOCKERFILE.read_text(encoding="utf-8")
        assert ",spark," in source, "the JRE step no longer keys on the extra"
        assert "jre" in source, (
            "pyspark without a JVM is an image that imports and then fails on "
            "the first action"
        )


class TestBothImagesAreBuiltWithIt:
    """One package, two images: the decision is made on one, the import on the
    other, so a deployment needs both or neither."""

    def test_the_build_script_forwards_both_arguments(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "RUNTIME_EXTRAS=${RUNTIME_EXTRAS:-}" in source
        assert "RUNNER_EXTRAS=${RUNNER_EXTRAS:-}" in source

    def test_each_argument_goes_to_its_own_image(self) -> None:
        lines = [
            line.split("#", 1)[0].strip()
            for line in BUILD_SCRIPT.read_text().splitlines()
        ]
        builds = [line for line in lines if line.startswith("build ")]
        runtime = next(line for line in builds if "workspace-runtime/Dockerfile" in line)
        runner = next(line for line in builds if "task-runner/Dockerfile" in line)
        assert "RUNTIME_BUILD_ARGS" in runtime and "RUNNER_BUILD_ARGS" not in runtime
        assert "RUNNER_BUILD_ARGS" in runner and "RUNTIME_BUILD_ARGS" not in runner
