"""The installer: what it refuses, what it cleans up, what it reports.

pip itself is replaced by a stub script for the cases that need one — installing
real wheels would make these tests a network test. Everything else here is the
real implementation: the platform-conflict refusal, the constraints file, the
overlay inspection, the size cap and the failure classification all run on real
files.

Every test states the platform baseline EXPLICITLY, because that is the contract
under test: the installer is told what the code location provides and must never
work it out from its own interpreter (a different, much smaller image).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from datacode_common.python_deps import InstallPlan

from src import deps_ops
from src.sqlmesh_ops import SqlMeshOpError

# What the code-location interpreter provides, for tests that do not care about
# the specifics. Deliberately unlike this interpreter's own packages: a test
# passing because the two happen to coincide would be the very confusion this
# parameter exists to remove.
_BASELINE = {"dagster": "1.13.21", "pandas": "2.2.0", "numpy": "1.26.4"}


def _params(*requirements: str, baseline: dict | None = None, **overrides) -> dict:
    plan = InstallPlan(
        requirements=tuple(requirements),
        fingerprint="fp",
        platform_packages=tuple(
            sorted((_BASELINE if baseline is None else baseline).items())
        ),
        index_url="https://index.invalid/simple",
        **overrides,
    )
    return plan.to_params()


def _dist_info(target: Path, name: str, version: str) -> None:
    """Create the metadata directory pip would leave behind."""
    info = target / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    (info / "RECORD").write_text(
        f"{name}.py,sha256=x,10\n{info.name}/METADATA,sha256=y,20\n"
    )
    (target / f"{name}.py").write_text("# payload\n")


@pytest.fixture()
def fake_pip(monkeypatch, tmp_path):
    """Replace the pip invocation with a recorder that fabricates a tree.

    The argv is asserted on, because the flags ARE the contract: no wheel-only
    flag means a source distribution gets a compiler it does not have, no
    `--constraint` means a transitive dependency can shadow a platform package,
    and no `--target` means the packages land in the image instead of the copy.
    """
    calls: list[list[str]] = []

    def _fake_run(requirements, target, workspace_path, plan, constraints, transcript):
        calls.append(list(requirements))
        transcript.write("Collecting polars")
        for name in requirements:
            _dist_info(Path(target), name.split(">")[0].split("=")[0], "1.2.0")

    monkeypatch.setattr(deps_ops, "_run_pip", _fake_run)
    return calls


def _workspace(tmp_path: Path) -> str:
    (tmp_path / "asset-dag").mkdir(parents=True)
    return str(tmp_path)


# ── the happy path ────────────────────────────────────────────────────────


def test_a_successful_install_reports_what_the_runtime_needs(fake_pip, tmp_path):
    path = _workspace(tmp_path)

    result = deps_ops.op_deps_sync(path, _params("polars"))

    assert result["packages"] == [{"name": "polars", "version": "1.2.0"}]
    assert result["fingerprint"] == "fp"
    # The interpreter fingerprint is what stops a tree built here from being put
    # on a different interpreter's path later.
    assert result["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert result["platform_tag"]
    assert result["size_bytes"] > 0
    assert "Collecting polars" in result["output_tail"]
    # The transcript is a file inside the copy: it is the live progress channel
    # AND what a user reads after a failure, so it must survive the task.
    transcript = Path(path) / ".datacode" / "deps" / "install.log"
    assert transcript.is_file()
    assert "Collecting polars" in transcript.read_text()


def test_the_manifest_is_not_written_by_the_runner(fake_pip, tmp_path):
    """One writer, on the runtime side.

    An interrupted install must not be able to leave a manifest claiming a tree
    that is not there.
    """
    path = _workspace(tmp_path)
    deps_ops.op_deps_sync(path, _params("polars"))
    assert not (Path(path) / ".datacode" / "deps" / "manifest.json").exists()


def test_a_platform_provided_package_is_skipped_not_installed(fake_pip, tmp_path):
    path = _workspace(tmp_path)

    result = deps_ops.op_deps_sync(path, _params("pandas>=2.0", "polars"))

    assert fake_pip == [["polars"]]
    assert [entry["name"] for entry in result["platform_provided"]] == ["pandas"]


def test_the_baseline_comes_from_the_plan_not_from_this_interpreter(
    fake_pip, tmp_path
):
    """The decisive property: this image is not the platform.

    `packaging` IS installed here and is NOT in the baseline, so introspection
    would skip it while the contract says install it. Under the old behaviour
    that skip produced an ImportError inside the user's own asset, and only in
    production — in development both interpreters are the same process.
    """
    path = _workspace(tmp_path)

    result = deps_ops.op_deps_sync(path, _params("packaging"))

    assert fake_pip == [["packaging"]], "a package the CODE LOCATION lacks is installed"
    assert result["platform_provided"] == []


def test_a_package_the_installer_lacks_but_the_platform_has_is_skipped(
    fake_pip, tmp_path
):
    """The mirror image, and the other production-only failure.

    `dagster` is not installed in this image. Introspection therefore concluded
    "the platform does not have it" and installed a second dagster into an
    overlay that comes FIRST on sys.path, silently replacing the one running the
    code location.
    """
    path = _workspace(tmp_path)

    result = deps_ops.op_deps_sync(path, _params("dagster>=1.8.0"))

    assert fake_pip == [], "the platform's dagster must never be shadowed"
    assert result["platform_provided"] == [
        {"name": "dagster", "version": "1.13.21"}
    ]


def test_a_plan_without_a_baseline_is_refused_before_pip_runs(fake_pip, tmp_path):
    """Fail closed: an empty baseline reads as "the platform has nothing"."""
    path = _workspace(tmp_path)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(path, _params("polars", baseline={}))

    assert excinfo.value.code == deps_ops.ERR_DEPS_INSTALL
    assert "no platform baseline" in str(excinfo.value)
    assert fake_pip == []


def test_a_declaration_the_platform_cannot_satisfy_is_refused(fake_pip, tmp_path):
    """The version the platform ships must be named, or there is no next step."""
    path = _workspace(tmp_path)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(path, _params("pandas<2.0"))

    # NOT the declaration code: the file is valid TOML, and telling the user it
    # "could not be read" sent them hunting for a syntax error.
    assert excinfo.value.code == deps_ops.ERR_DEPS_PLATFORM_CONFLICT
    assert "pandas" in str(excinfo.value) and "2.2.0" in str(excinfo.value)
    assert fake_pip == [], "nothing may be installed once the plan is refused"


def test_an_empty_plan_is_a_no_op_not_an_error(fake_pip, tmp_path):
    path = _workspace(tmp_path)
    result = deps_ops.op_deps_sync(path, _params("pandas>=2.0"))
    assert result["packages"] == []
    assert fake_pip == []


def test_malformed_params_are_refused_before_anything_runs(tmp_path):
    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(_workspace(tmp_path), {"requirements": "polars"})
    assert excinfo.value.code == deps_ops.ERR_DEPS_DECLARATION


# ── the constraints file ──────────────────────────────────────────────────


def test_the_baseline_is_written_as_constraints_next_to_the_overlay(
    fake_pip, tmp_path
):
    path = _workspace(tmp_path)
    deps_ops.op_deps_sync(path, _params("polars"))

    constraints = Path(path) / ".datacode" / "deps" / "platform-constraints.txt"
    assert constraints.is_file()
    body = constraints.read_text()
    # Every baseline package, so TRANSITIVE resolution is bounded too — a
    # subset would leave exactly the packages nobody declared (numpy, pyarrow)
    # free to resolve to a shadowing version.
    for pin in ("dagster==1.13.21", "pandas==2.2.0", "numpy==1.26.4"):
        assert pin in body


def test_a_transitive_baseline_version_is_removed_from_the_overlay(
    monkeypatch, tmp_path
):
    """What the constraints make the NORMAL outcome.

    pip resolves a dependency the platform already has to the pinned version
    and unpacks an identical copy. Identical or not, that copy comes first on
    sys.path — and would stay behind after a platform upgrade, since nothing
    reinstalls an overlay whose fingerprint did not change.
    """
    path = _workspace(tmp_path)

    def _pull_numpy(requirements, target, workspace_path, plan, constraints, transcript):
        _dist_info(Path(target), "polars", "1.2.0")
        _dist_info(Path(target), "numpy", "1.26.4")  # == the baseline

    monkeypatch.setattr(deps_ops, "_run_pip", _pull_numpy)

    result = deps_ops.op_deps_sync(path, _params("polars"))

    assert [entry["name"] for entry in result["packages"]] == ["polars"]
    overlay = Path(path) / ".datacode" / "deps" / "site-packages"
    assert not (overlay / "numpy-1.26.4.dist-info").exists()
    assert not (overlay / "numpy.py").exists()
    assert (overlay / "polars.py").is_file(), "the declared package survives"
    # Reported as empty: with the invariant enforced there is nothing to warn
    # about, so the manifest field stops being a field anyone must interpret.
    assert result["shadowed"] == []


def test_a_record_entry_pointing_outside_the_overlay_is_ignored(
    monkeypatch, tmp_path
):
    """RECORD is data that arrived with a downloaded archive.

    Honouring `../../x` in it would turn this cleanup into an arbitrary delete
    running with the sandbox's write access to the whole working copy.
    """
    path = _workspace(tmp_path)
    victim = Path(path) / "asset-dag" / "definitions.py"
    victim.write_text("defs = None\n")

    def _evil(requirements, target, workspace_path, plan, constraints, transcript):
        _dist_info(Path(target), "numpy", "1.26.4")
        info = Path(target) / "numpy-1.26.4.dist-info"
        (info / "RECORD").write_text("../../asset-dag/definitions.py,sha256=x,10\n")

    monkeypatch.setattr(deps_ops, "_run_pip", _evil)

    deps_ops.op_deps_sync(path, _params("polars"))

    assert victim.is_file(), "a RECORD entry must not reach outside the overlay"


# ── cleaning up ───────────────────────────────────────────────────────────


def test_a_previous_tree_is_removed_before_installing(fake_pip, tmp_path):
    """The tree must describe ONE declaration, never the union of two."""
    path = _workspace(tmp_path)
    target = Path(path) / ".datacode" / "deps" / "site-packages"
    _dist_info(target, "leftover", "0.1.0")

    result = deps_ops.op_deps_sync(path, _params("polars"))

    assert [entry["name"] for entry in result["packages"]] == ["polars"]
    assert not (target / "leftover-0.1.0.dist-info").exists()


def test_a_failed_install_leaves_no_partial_tree(monkeypatch, tmp_path):
    """A half tree on PYTHONPATH is an import error inside user code."""
    path = _workspace(tmp_path)

    def _explode(requirements, target, workspace_path, plan, constraints, transcript):
        _dist_info(Path(target), "half", "0.1.0")
        raise SqlMeshOpError(deps_ops.ERR_DEPS_INSTALL, "boom")

    monkeypatch.setattr(deps_ops, "_run_pip", _explode)

    with pytest.raises(SqlMeshOpError):
        deps_ops.op_deps_sync(path, _params("polars"))

    assert not (Path(path) / ".datacode" / "deps" / "site-packages").exists()


def test_a_tree_over_the_size_limit_is_refused_and_removed(fake_pip, tmp_path):
    path = _workspace(tmp_path)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(path, _params("polars", max_bytes=1))

    assert excinfo.value.code == deps_ops.ERR_DEPS_TOO_LARGE
    assert not (Path(path) / ".datacode" / "deps" / "site-packages").exists()


def test_a_surviving_shadow_fails_the_install(monkeypatch, tmp_path):
    """Unreachable through any declaration — so it is a defect, not a warning.

    A DIFFERENT version of a baseline package can only appear if the
    constraints did not reach pip, which means the two images are out of step.
    Recording it and carrying on is what made "the platform behaves differently
    in my workspace" take an afternoon to diagnose.
    """
    path = _workspace(tmp_path)

    def _pull_old_numpy(
        requirements, target, workspace_path, plan, constraints, transcript
    ):
        _dist_info(Path(target), "polars", "1.2.0")
        _dist_info(Path(target), "numpy", "1.20.0")  # != the baseline

    monkeypatch.setattr(deps_ops, "_run_pip", _pull_old_numpy)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(path, _params("polars"))

    assert excinfo.value.code == deps_ops.ERR_DEPS_SHADOWED
    assert "numpy" in str(excinfo.value)
    assert not (Path(path) / ".datacode" / "deps" / "site-packages").exists()


def test_shadowing_a_protected_package_names_the_consequence(monkeypatch, tmp_path):
    path = _workspace(tmp_path)

    def _pull_grpcio(
        requirements, target, workspace_path, plan, constraints, transcript
    ):
        _dist_info(Path(target), "polars", "1.2.0")
        _dist_info(Path(target), "grpcio", "1.0.0")

    monkeypatch.setattr(deps_ops, "_run_pip", _pull_grpcio)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path, _params("polars", baseline={"grpcio": "1.62.0", "dagster": "1.13.21"})
        )

    assert excinfo.value.code == deps_ops.ERR_DEPS_SHADOWED
    assert "grpcio" in str(excinfo.value)


# ── failure classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ERROR: No matching distribution found for polars", "no version matching"),
        ("ResolutionImpossible: conflicting versions", "cannot be satisfied together"),
        ("ERROR: Could not find a version that satisfies", "no version matching"),
        ("proxy CONNECT tunnel failed", "unreachable from the sandbox"),
        ("something nobody anticipated", "pip exited with code 1"),
    ],
)
def test_pip_failures_are_classified_before_the_raw_output(output, expected):
    """The raw output is kept, but a sentence naming the next action leads."""
    message = deps_ops._classify_failure(output, 1)
    assert expected in message
    # pip's own text is frequently the only place the version pair appears.
    assert output in message


def test_a_conflict_against_a_baseline_pin_is_worded_differently():
    """The bound the user cannot move is on the platform's side.

    Telling them to "relax one of the bounds" would send them looking for a
    bound they never wrote.

    This is the second of the two ways pip cites such a pin: by VALUE, with no
    "(constraint)" marker and no filename. Recognising only the marker would
    miss it; recognising only the filename misses real pip output entirely,
    since the conflict report names no file.
    """
    message = deps_ops._classify_failure(
        "ResolutionImpossible: the conflict is caused by "
        "the user requested numpy==1.26.4",
        1,
        {"numpy": "1.26.4"},
    )
    # Reaching this wording means the installer already released every pin it
    # was allowed to release, so it names the class of pin that stays and the
    # two people who can act on it.
    assert "cannot yield" in message
    assert "deps.relaxable_packages" in message


# ── real pip, real wheel-free flags ───────────────────────────────────────


def test_the_pip_invocation_carries_the_flags_that_make_it_safe(tmp_path):
    """A stub 'pip' records the argv the real one would have received.

    Asserted end to end rather than on a rendered string: the flags decide
    whether a source distribution can execute setup.py in the sandbox, whether
    a transitive dependency can shadow a platform package, and where the
    packages land.
    """
    path = _workspace(tmp_path)
    recorder = tmp_path / "argv.json"
    stub = tmp_path / "python-stub"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(recorder)!r}, 'w').write(json.dumps(sys.argv))\n"
        "sys.exit(0)\n"
    )
    stub.chmod(0o755)

    import subprocess

    original = subprocess.Popen

    def _popen(argv, **kwargs):
        # Swap the interpreter, keep every argument the module built.
        return original([str(stub), *argv[1:]], **kwargs)

    import unittest.mock as mock

    with mock.patch.object(subprocess, "Popen", _popen):
        # The stub exits 0 and writes no tree, so this "succeeds" with an empty
        # overlay. Irrelevant here — what is under test is the argv.
        result = deps_ops.op_deps_sync(path, _params("polars>=1.0"))
    assert result["packages"] == []

    argv = json.loads(recorder.read_text())
    assert "--only-binary=:all:" in argv, "a source distribution has no compiler here"
    assert "--isolated" in argv, "a user pip.conf must not redirect the index"
    assert "--index-url" in argv and "https://index.invalid/simple" in argv
    target_index = argv.index("--target")
    assert argv[target_index + 1].endswith(
        os.path.join(".datacode", "deps", "site-packages")
    )
    # The flag that bounds transitive resolution, pointed at the generated file.
    constraint_index = argv.index("--constraint")
    assert argv[constraint_index + 1].endswith("platform-constraints.txt")


# ── diagnosing an unsatisfiable resolution ────────────────────────────────

# Verbatim from a real failure: pip attributes the platform's own pins to "the
# user", and cites them by VALUE — the constraints file is named nowhere in the
# conflict report.
_REAL_PIP_CONFLICT = """Collecting feast<0.35.0,>=0.34.0
ERROR: Cannot install feast because these package versions have conflicting dependencies.

The conflict is caused by:
    feast 0.34.1 depends on numpy<1.25 and >=1.22
    The user requested (constraint) numpy==2.5.2

To fix this you could try to:
1. loosen the range of package versions you've specified
"""


def _failing_pip(monkeypatch, output: str, exit_code: int = 1) -> list[list[str]]:
    """A pip that writes *output* and exits non-zero. Returns the recorded argv."""
    recorded: list[list[str]] = []

    class _Process:
        def __init__(self, argv):
            recorded.append(list(argv))
            self.stdout = iter(output.splitlines(keepends=True))

        def wait(self):
            return exit_code

    monkeypatch.setattr(
        deps_ops.subprocess, "Popen", lambda argv, **kwargs: _Process(argv)
    )
    return recorded


def _no_probe(monkeypatch) -> None:
    """The diagnosis produced nothing — the fallback path is what is under test."""
    monkeypatch.setattr(deps_ops, "_probe_resolution", lambda *args, **kwargs: None)


def test_a_real_pip_conflict_report_is_recognised_as_a_platform_pin(
    monkeypatch, tmp_path
):
    """THE case the wording was written for, with pip's own output.

    pip blames "the user" for a bound that came from the platform baseline, and
    cites it by value without naming the file. Recognising it only by the
    constraints filename meant this branch never fired on a real failure, and
    the user was told to relax a bound they never wrote.
    """
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, _REAL_PIP_CONFLICT)
    _no_probe(monkeypatch)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path,
            _params("feast[postgres]<0.35.0,>=0.34.0", baseline={"numpy": "2.5.2"}),
        )

    assert excinfo.value.code == deps_ops.ERR_DEPS_INSTALL
    assert "cannot yield" in str(excinfo.value)
    # Not the generic instruction, which points at a bound the user never wrote.
    assert "relax one of the bounds" not in str(excinfo.value)


def test_a_conflict_the_baseline_is_not_part_of_is_worded_generically(
    monkeypatch, tmp_path
):
    """The negative control: two declared packages fighting each other.

    Blaming the platform for every unsatisfiable resolution would be its own
    kind of misdirection — here the user CAN move both bounds.
    """
    path = _workspace(tmp_path)
    _failing_pip(
        monkeypatch,
        "ERROR: ResolutionImpossible: the conflict is caused by\n"
        "    feast 0.34.1 depends on numpy<1.25\n",
    )
    _no_probe(monkeypatch)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path, _params("feast", "numpy<2", baseline={"pandas": "2.3.3"})
        )

    assert "cannot be satisfied together" in str(excinfo.value)
    assert "the platform's version" not in str(excinfo.value)


def test_the_transcript_says_the_platform_pins_instead_of_the_user(tmp_path):
    """The same version, thirty lines apart, must not have two owners."""
    from src.deps_ops import _translate_pip_voice

    assert _translate_pip_voice("    The user requested (constraint) numpy==2.5.2") == (
        "    the platform pins numpy==2.5.2 (from platform-constraints.txt)"
    )
    # Anything pip says that is not that sentence is passed through untouched.
    assert _translate_pip_voice("Collecting feast") == "Collecting feast"


def test_a_resolution_conflict_is_diagnosed_into_a_verified_edit(
    monkeypatch, tmp_path
):
    """What the user gets: the blocking package, and an edit pip resolved.

    The blocking package is one the declaration never mentions, so the message
    has to name it — and the rewrite has to be one the installer actually
    tried, not a version someone guessed would probably work.
    """
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, _REAL_PIP_CONFLICT)
    probes: list[tuple[str, bool]] = []

    def _probe(requirements, *, constraints, **kwargs):
        probes.append((requirements[0], constraints is not None))
        if constraints is None:
            # Without the baseline, feast 0.34.1 would drag numpy backwards.
            return {"install": [{"metadata": {"name": "numpy", "version": "1.24.3"}}]}
        # With it, dropping the ceiling resolves — to 0.40.1.
        return {"install": [{"metadata": {"name": "feast", "version": "0.40.1"}}]}

    monkeypatch.setattr(deps_ops, "_probe_resolution", _probe)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path,
            _params("feast[postgres]<0.35.0,>=0.34.0", baseline={"numpy": "2.5.2"}),
        )

    assert excinfo.value.code == deps_ops.ERR_DEPS_PLATFORM_TRANSITIVE
    # Both probes, in the order that makes the answer possible: what it wants,
    # then what would work.
    assert probes == [
        ("feast[postgres]<0.35.0,>=0.34.0", False),
        ("feast[postgres]>=0.34.0", True),
    ]
    conflict = excinfo.value.detail["transitive_conflicts"][0]
    assert conflict["name"] == "feast", "the line to edit is the declared package"
    assert conflict["blockers"] == [
        {
            "name": "numpy",
            # No requires_dist in this report, so the only sound evidence is
            # that the resolution moved numpy BACKWARDS — something stated a
            # ceiling. Expressed as a ceiling rather than as a bare version.
            "required": "<=1.24.3",
            "platform_version": "2.5.2",
            "required_by": "",
        }
    ]
    assert conflict["suggested"] == "feast[postgres]>=0.34.0"
    assert conflict["resolved_version"] == "0.40.1"
    message = str(excinfo.value)
    assert "numpy" in message and "feast[postgres]>=0.34.0" in message


def test_an_unfixable_declaration_says_none_fits_rather_than_guessing(
    monkeypatch, tmp_path
):
    """Every release collides with the baseline. That is an answer too."""
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, _REAL_PIP_CONFLICT)

    def _probe(requirements, *, constraints, **kwargs):
        if constraints is None:
            return {"install": [{"metadata": {"name": "numpy", "version": "1.24.3"}}]}
        return None  # even with the ceiling dropped, nothing resolves

    monkeypatch.setattr(deps_ops, "_probe_resolution", _probe)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path,
            _params("feast[postgres]<0.35.0,>=0.34.0", baseline={"numpy": "2.5.2"}),
        )

    conflict = excinfo.value.detail["transitive_conflicts"][0]
    assert conflict["suggested"] == ""
    # The dead end names the pin behind it. Nothing is yieldable in this plan, so
    # numpy is protected and the message says which package and why — "no
    # release fits" on its own leaves nobody able to act.
    assert conflict["protected"] == ["numpy"]
    assert "numpy" in str(excinfo.value)
    assert "ask an operator" in str(excinfo.value)


def test_a_failure_that_is_not_a_resolution_is_not_diagnosed(monkeypatch, tmp_path):
    """A missing wheel cannot be fixed by rewriting a bound.

    Probing it would double the wait and then say nothing, so the diagnosis is
    gated on pip actually failing to resolve.
    """
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, "ERROR: No matching distribution found for feast==9")

    def _explode(*args, **kwargs):
        raise AssertionError("a non-resolution failure must not be diagnosed")

    monkeypatch.setattr(deps_ops, "_probe_resolution", _explode)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(path, _params("feast==9", baseline={"numpy": "2.5.2"}))

    assert excinfo.value.code == deps_ops.ERR_DEPS_INSTALL
    assert "no version matching the declaration" in str(excinfo.value)


# ── yielding a platform pin ───────────────────────────────────────────────

# Verbatim from the failure that motivated the yield mechanism: the ONLY real
# blocker is tenacity, a pure-Python leaf, and refusing the whole declaration
# over it is what made "install feast" impossible.
_TENACITY_CONFLICT = """Collecting feast[postgres]<0.66.0,>=0.60.0
ERROR: Cannot install feast because these package versions have conflicting dependencies.

The conflict is caused by:
    feast 0.65.0 depends on tenacity<9 and >=7
    feast 0.64.0 depends on tenacity<9 and >=7
    The user requested (constraint) tenacity==9.1.4
"""


def _two_pass_pip(monkeypatch, *, second_succeeds: bool) -> list[dict]:
    """A pip that refuses the first attempt and is watched on the second.

    Records, per attempt, the constraints file's contents — which is the whole
    contract under test: the retry differs from the first attempt ONLY in which
    pins it carries.
    """
    attempts: list[dict] = []

    def _fake_run(requirements, target, workspace_path, plan, constraints, transcript):
        body = Path(constraints).read_text()
        attempts.append({"pins": body, "requirements": list(requirements)})
        if len(attempts) == 1:
            for line in _TENACITY_CONFLICT.splitlines():
                transcript.write(line)
            return 1
        if not second_succeeds:
            transcript.write("ERROR: ResolutionImpossible")
            return 1
        _dist_info(Path(target), "feast", "0.65.0")
        _dist_info(Path(target), "tenacity", "8.5.0")
        return 0

    monkeypatch.setattr(deps_ops, "_run_pip", _fake_run)
    return attempts


def test_a_conflict_over_a_yieldable_pin_is_retried_without_it(monkeypatch, tmp_path):
    """The case the whole mechanism exists for.

    feast needs tenacity<9; the platform pins 9.1.4. tenacity is pure Python,
    so overriding it affects this working copy and nothing else — refusing the
    declaration over it is the wrong trade, and telling the user "no release of
    feast fits this platform" is not even true.
    """
    path = _workspace(tmp_path)
    attempts = _two_pass_pip(monkeypatch, second_succeeds=True)

    result = deps_ops.op_deps_sync(
        path,
        _params(
            "feast[postgres]<0.66.0,>=0.60.0",
            baseline={"tenacity": "9.1.4", "numpy": "2.5.2"},
            relaxable_packages=("tenacity",),
        ),
    )

    assert len(attempts) == 2, "the full baseline is always tried first"
    assert "tenacity==9.1.4" in attempts[0]["pins"]
    assert "tenacity==9.1.4" not in attempts[1]["pins"]
    # The pin that may NOT move stays pinned in both attempts.
    assert "numpy==2.5.2" in attempts[1]["pins"]

    assert result["relaxed"] == [
        {
            "name": "tenacity",
            "platform_version": "9.1.4",
            "overlay_version": "8.5.0",
            # Read from pip's own report, which is the only place it is stated.
            "required_by": "feast",
        }
    ]
    # The override is in the tree, and is NOT reported as a shadow: it is the
    # intended outcome, and detect_shadowed has to be told so or the mechanism
    # would fail its own post-install assertion.
    assert {entry["name"] for entry in result["packages"]} == {"feast", "tenacity"}
    assert result["shadowed"] == []


def test_a_conflict_over_a_protected_pin_is_not_retried(monkeypatch, tmp_path):
    """numpy is ABI-bound: yielding it breaks the platform, not the workspace.

    The negative control for the whole design — without it "relax on conflict"
    would be indistinguishable from "no constraints at all".
    """
    path = _workspace(tmp_path)
    attempts: list[str] = []

    def _fake_run(requirements, target, workspace_path, plan, constraints, transcript):
        attempts.append(Path(constraints).read_text())
        # The conflict marker matters: the yield path is gated on pip having
        # failed to RESOLVE, so an output without it would make this test pass
        # for the wrong reason (no yield attempted at all, rather than one
        # refused).
        transcript.write("ERROR: Cannot install feast because these package "
                         "versions have conflicting dependencies.")
        transcript.write("    feast 0.34.1 depends on numpy<1.25")
        transcript.write("    The user requested (constraint) numpy==2.5.2")
        return 1

    monkeypatch.setattr(deps_ops, "_run_pip", _fake_run)
    monkeypatch.setattr(deps_ops, "_probe_resolution", lambda *a, **k: None)

    with pytest.raises(SqlMeshOpError):
        deps_ops.op_deps_sync(
            path,
            _params(
                "feast<0.35.0,>=0.34.0",
                baseline={"numpy": "2.5.2", "tenacity": "9.1.4"},
                relaxable_packages=("tenacity",),
            ),
        )

    assert len(attempts) == 1, "a protected pin is never released"
    assert "numpy==2.5.2" in attempts[0]


def test_a_yield_is_attempted_only_once(monkeypatch, tmp_path):
    """Every yieldable pin goes into the retry, so a third attempt answers
    nothing: what is left can only involve a pin that may not move."""
    path = _workspace(tmp_path)
    attempts = _two_pass_pip(monkeypatch, second_succeeds=False)
    monkeypatch.setattr(deps_ops, "_probe_resolution", lambda *a, **k: None)

    with pytest.raises(SqlMeshOpError):
        deps_ops.op_deps_sync(
            path,
            _params(
                "feast[postgres]<0.66.0,>=0.60.0",
                baseline={"tenacity": "9.1.4"},
                relaxable_packages=("tenacity",),
            ),
        )

    assert len(attempts) == 2


def test_a_plan_without_yieldable_pins_behaves_exactly_as_before(
    monkeypatch, tmp_path
):
    """Fail closed. An installer talking to an older runtime receives no
    yieldable set, and must refuse rather than override a platform package
    nobody approved."""
    path = _workspace(tmp_path)
    attempts = _two_pass_pip(monkeypatch, second_succeeds=True)
    monkeypatch.setattr(deps_ops, "_probe_resolution", lambda *a, **k: None)

    with pytest.raises(SqlMeshOpError):
        deps_ops.op_deps_sync(
            path,
            _params(
                "feast[postgres]<0.66.0,>=0.60.0",
                baseline={"tenacity": "9.1.4"},
            ),
        )

    assert len(attempts) == 1


def test_the_diagnosis_probes_against_the_pins_that_cannot_move(
    monkeypatch, tmp_path
):
    """Otherwise "no release fits" is printed for a declaration that fits.

    By the time a diagnosis runs, the yieldable pins have already been released
    and retried. Probing against the FULL baseline would answer a question the
    installer already answered — and answer it with a verdict the user cannot
    act on.
    """
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, _TENACITY_CONFLICT)
    probed: list[str] = []

    def _probe(requirements, *, constraints, **kwargs):
        if constraints is not None:
            probed.append(Path(constraints).read_text())
        return None

    monkeypatch.setattr(deps_ops, "_probe_resolution", _probe)

    with pytest.raises(SqlMeshOpError):
        deps_ops.op_deps_sync(
            path,
            _params(
                "feast[postgres]<0.66.0,>=0.60.0",
                # tenacity is yieldable, but numpy — the pin the report does not
                # even mention — is not, so the install still fails here.
                baseline={"tenacity": "9.1.4", "numpy": "2.5.2"},
                relaxable_packages=("tenacity",),
            ),
        )

    assert probed, "the ceiling-dropped probe must run"
    assert "tenacity==9.1.4" not in probed[0]
    assert "numpy==2.5.2" in probed[0]


def test_a_dead_end_names_the_protected_blocker(monkeypatch, tmp_path):
    """"No release fits" without a name is unactionable for everyone."""
    path = _workspace(tmp_path)
    _failing_pip(monkeypatch, _REAL_PIP_CONFLICT)

    def _probe(requirements, *, constraints, **kwargs):
        if constraints is None:
            return {
                "install": [
                    {
                        "metadata": {
                            "name": "feast",
                            "version": "0.34.1",
                            "requires_dist": ["numpy<1.25,>=1.22"],
                        }
                    },
                    {"metadata": {"name": "numpy", "version": "1.24.3"}},
                ]
            }
        return None

    monkeypatch.setattr(deps_ops, "_probe_resolution", _probe)

    with pytest.raises(SqlMeshOpError) as excinfo:
        deps_ops.op_deps_sync(
            path,
            _params(
                "feast[postgres]<0.35.0,>=0.34.0",
                baseline={"numpy": "2.5.2"},
                relaxable_packages=("tenacity",),
            ),
        )

    conflict = excinfo.value.detail["transitive_conflicts"][0]
    assert conflict["blockers"] == [
        {
            "name": "numpy",
            # The RANGE, not a version pip happened to float to.
            "required": "<1.25,>=1.22",
            "platform_version": "2.5.2",
            "required_by": "feast",
        }
    ]
    assert conflict["protected"] == ["numpy"]
    assert "deps.relaxable_packages" in str(excinfo.value) or "operator" in str(
        excinfo.value
    )
