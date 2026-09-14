"""Contract tests for the working-copy Python dependency layer.

Two properties carry the whole feature:

* the fingerprint is a function of MEANING, not formatting — otherwise every
  reformatting of ``pyproject.toml`` reinstalls the tree and the "already
  installed, skip" fast path never fires;
* a declaration that would shadow a platform package is REFUSED, not installed
  — the overlay always wins on ``sys.path``, so an accepted conflict is a
  broken code location with no way back.
"""

from __future__ import annotations

import pytest

from datacode_common.python_deps import (
    PROTECTED_DISTRIBUTIONS,
    STATE_DRIFTED,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_INCOMPATIBLE,
    STATE_MISSING,
    STATE_READY,
    DepsDeclarationError,
    DepsManifest,
    InstallPlan,
    PlatformCoreConflict,
    TransitiveConflict,
    baseline_constraints,
    blockers_from_resolution,
    compiled_distributions,
    constraints_path,
    detect_shadowed,
    drop_baseline_equivalents,
    find_platform_conflicts,
    installed_distributions,
    interpreter_mismatch,
    interpreter_platform_tag,
    interpreter_python_version,
    manifest_path,
    manifest_state,
    parse_declaration,
    pins_implicated_in_conflict,
    relax_upper_bounds,
    relaxable_baseline,
    requirement_name,
    site_packages_path,
    split_platform_provided,
)


def _toml(*requirements: str) -> str:
    body = ", ".join(f'"{item}"' for item in requirements)
    return f'[project]\nname = "assets"\ndependencies = [{body}]\n'


# ── declaration parsing ───────────────────────────────────────────────────


def test_three_ways_of_declaring_nothing_agree():
    absent = parse_declaration('[project]\nname = "assets"\n')
    empty = parse_declaration(_toml())
    no_project = parse_declaration('[tool.dagster]\nmodule_name = "defs"\n')

    assert absent.is_empty
    assert absent.fingerprint == empty.fingerprint == no_project.fingerprint


def test_fingerprint_ignores_order_case_whitespace_and_duplicates():
    a = parse_declaration(_toml("Requests>=2.31", "polars[all]>=1.0,<2"))
    b = parse_declaration(
        _toml("polars[ALL] <2 , >=1.0", "  requests >= 2.31  ", "requests>=2.31")
    )

    assert a.fingerprint == b.fingerprint
    assert a.requirements == ("polars[all]<2,>=1.0", "requests>=2.31")


def test_fingerprint_changes_when_a_bound_changes():
    assert (
        parse_declaration(_toml("requests>=2.31")).fingerprint
        != parse_declaration(_toml("requests>=2.32")).fingerprint
    )


def test_markers_survive_normalisation():
    # Single-quoted TOML literal string: the marker itself contains the double
    # quotes PEP 508 requires around the version.
    decl = parse_declaration(
        '[project]\ndependencies = [\'pandas>=2.0; python_version >= "3.11"\']\n'
    )
    assert decl.requirements == ('pandas>=2.0; python_version >= "3.11"',)
    assert requirement_name(decl.requirements[0]) == "pandas"


@pytest.mark.parametrize(
    "text",
    [
        "[project\nname = 'x'",
        '[project]\ndependencies = "requests"\n',
        "[project]\ndependencies = [42]\n",
        '[project]\ndependencies = ["not a requirement=="]\n',
        '[project]\ndependencies = [""]\n',
    ],
)
def test_malformed_declarations_name_the_offending_file(text):
    with pytest.raises(DepsDeclarationError) as excinfo:
        parse_declaration(text)
    assert "pyproject.toml" in str(excinfo.value)


def test_direct_url_requirements_are_refused():
    """A URL lets a file in the working copy choose the fetch target.

    The sandbox egress allow-list comes from platform configuration for exactly
    this reason; honouring a URL here hands that choice back to whoever can
    edit the repository.
    """
    with pytest.raises(DepsDeclarationError) as excinfo:
        parse_declaration(_toml("evil @ https://example.invalid/e-1.0-py3-none-any.whl"))
    assert "URL" in str(excinfo.value)


def test_too_many_requirements_is_refused():
    with pytest.raises(DepsDeclarationError):
        parse_declaration(_toml(*[f"pkg-{i}" for i in range(201)]))


# ── platform packages ─────────────────────────────────────────────────────


def test_platform_provided_requirements_are_skipped_not_installed():
    decl = parse_declaration(_toml("dagster>=1.13", "polars>=1.0"))
    to_install, provided = split_platform_provided(decl, {"dagster": "1.13.21"})

    assert to_install == ("polars>=1.0",)
    assert provided == ({"name": "dagster", "version": "1.13.21"},)


def test_unsatisfiable_platform_package_is_refused_and_names_the_version():
    decl = parse_declaration(_toml("dagster>=2.0"))
    with pytest.raises(PlatformCoreConflict) as excinfo:
        split_platform_provided(decl, {"dagster": "1.13.21"})

    assert excinfo.value.first.name == "dagster"
    # Without the shipped version in the message the user has no next step.
    assert "1.13.21" in str(excinfo.value)
    # And without a rewrite they have to work out the syntax themselves.
    assert excinfo.value.first.suggested == "dagster>=1.13.21"


def test_every_conflict_is_reported_not_only_the_first():
    """Four reloads to find four conflicts is the behaviour this prevents."""
    decl = parse_declaration(_toml("dagster>=2.0", "pyarrow<15", "polars>=1.0"))
    with pytest.raises(PlatformCoreConflict) as excinfo:
        split_platform_provided(
            decl, {"dagster": "1.13.21", "pyarrow": "25.0.1"}
        )

    assert [item.name for item in excinfo.value.conflicts] == ["dagster", "pyarrow"]
    text = str(excinfo.value)
    assert "dagster>=1.13.21" in text and "pyarrow>=25.0.1" in text


def test_a_conflict_suggests_a_lower_bound_not_a_pin():
    """A pin would turn today's fix into the next image upgrade's conflict."""
    conflicts = find_platform_conflicts(
        parse_declaration(_toml("pyarrow<15.0.0,>=14.0.0")), {"pyarrow": "25.0.1"}
    )
    assert [item.suggested for item in conflicts] == ["pyarrow>=25.0.1"]


def test_a_compatible_declaration_has_no_conflicts():
    assert (
        find_platform_conflicts(
            parse_declaration(_toml("dagster>=1.8.0", "polars>=1.0")),
            {"dagster": "1.13.21"},
        )
        == ()
    )


def test_unconstrained_platform_package_is_satisfied_by_any_version():
    decl = parse_declaration(_toml("sqlmesh"))
    to_install, provided = split_platform_provided(decl, {"sqlmesh": "0.145.0"})
    assert (to_install, provided) == ((), ({"name": "sqlmesh", "version": "0.145.0"},))


def test_prerelease_platform_version_satisfies_a_lower_bound():
    decl = parse_declaration(_toml("dagster>=1.13"))
    _, provided = split_platform_provided(decl, {"dagster": "1.14.0rc1"})
    assert provided == ({"name": "dagster", "version": "1.14.0rc1"},)


def test_transitive_shadowing_separates_risky_from_fatal():
    overlay = {"grpcio": "1.60.0", "polars": "1.2.0", "urllib3": "2.2.0"}
    platform = {"grpcio": "1.62.0", "urllib3": "2.0.7", "dagster": "1.13.21"}

    shadowed, protected = detect_shadowed(overlay, platform)

    assert shadowed == (
        {"name": "grpcio", "version": "1.60.0"},
        {"name": "urllib3", "version": "2.2.0"},
    )
    # Both fail the install; the split only decides how the failure is worded,
    # because grpcio breaks the code location server itself.
    assert protected == ("grpcio",)
    assert "grpcio" in PROTECTED_DISTRIBUTIONS


# ── constraints: the transitive half of the invariant ─────────────────────


def test_constraints_pin_every_baseline_package():
    """The pin is what makes a transitive conflict impossible.

    Without it, declaring `polars` alone lets pip resolve numpy 2.x into the
    overlay, shadow the platform's numpy, and break pandas at ABI level inside
    a code location.
    """
    text = baseline_constraints({"numpy": "1.26.4", "dagster": "1.13.21"})
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == ["dagster==1.13.21", "numpy==1.26.4"]


def test_constraints_skip_versions_no_index_can_satisfy():
    """An editable or local build matches nothing, and would fail every install."""
    text = baseline_constraints(
        {"datacode-common": "0.1.0.dev0+local.build", "polars": "1.2.0", "x": ""}
    )
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == ["polars==1.2.0"]


def test_baseline_equivalents_are_dropped_from_the_overlay():
    """Equivalent or not, a second copy shadows the platform's.

    Under the constraints above this is the NORMAL outcome for a transitive
    dependency the platform already has: pip resolves it to the pinned version
    and unpacks an identical tree.
    """
    redundant = drop_baseline_equivalents(
        {"numpy": "1.26.4", "polars": "1.2.0", "urllib3": "2.2.0"},
        {"numpy": "1.26.4", "urllib3": "2.0.7"},
    )
    # numpy matches and goes; urllib3 differs, so it survives to be reported as
    # a hard failure by detect_shadowed.
    assert redundant == ("numpy",)


def test_an_overlay_with_only_new_packages_is_clean():
    assert drop_baseline_equivalents({"polars": "1.2.0"}, {"numpy": "1.26.4"}) == ()
    assert detect_shadowed({"polars": "1.2.0"}, {"numpy": "1.26.4"}) == ((), ())


def test_installed_distributions_sees_this_interpreter():
    found = installed_distributions()
    assert found.get("packaging")
    # The most important entry: shadowing it would replace these contracts.
    assert "datacode-common" in found


# ── manifest and interpreter compatibility ────────────────────────────────


def _manifest(fingerprint: str, **overrides) -> DepsManifest:
    base = {
        "state": STATE_READY,
        "fingerprint": fingerprint,
        "python_version": interpreter_python_version(),
        "platform_tag": interpreter_platform_tag(),
    }
    base.update(overrides)
    return DepsManifest(**base)


def test_manifest_round_trips_through_json():
    original = _manifest(
        "abc",
        packages=({"name": "polars", "version": "1.2.0"},),
        platform_provided=({"name": "dagster", "version": "1.13.21"},),
        size_bytes=1234,
        source="ui",
        actor_user_id="u1",
    )
    assert DepsManifest.from_json(original.to_json()) == original


def test_unreadable_manifest_degrades_to_reinstall_never_raises():
    """A manifest is cache, not input.

    Raising on a field a future version added would turn a reload into
    something that can never succeed again; concluding "drifted" costs one
    reinstall.
    """
    restored = DepsManifest.from_dict(
        {"state": "ready", "fingerprint": "x", "packages": "junk", "future_key": 1}
    )
    assert restored.packages == ()
    assert manifest_state(parse_declaration(_toml("polars")), restored) == STATE_DRIFTED


@pytest.mark.parametrize(
    ("declaration", "manifest", "overlay_present", "expected"),
    [
        (_toml(), None, False, STATE_EMPTY),
        (_toml("polars"), None, False, STATE_MISSING),
        (_toml("polars"), "match", False, STATE_MISSING),
        (_toml("polars"), "match", True, STATE_READY),
        (_toml("polars"), "other", True, STATE_DRIFTED),
        (_toml("polars"), "failed", True, STATE_FAILED),
        (_toml("polars"), "old-python", True, STATE_INCOMPATIBLE),
    ],
)
def test_manifest_state_covers_every_conclusion(
    declaration, manifest, overlay_present, expected
):
    decl = parse_declaration(declaration)
    stored = None
    if manifest == "match":
        stored = _manifest(decl.fingerprint)
    elif manifest == "other":
        stored = _manifest("stale")
    elif manifest == "failed":
        stored = _manifest(decl.fingerprint, state=STATE_FAILED)
    elif manifest == "old-python":
        stored = _manifest(decl.fingerprint, python_version="3.9")

    assert manifest_state(decl, stored, overlay_present=overlay_present) == expected


def test_failed_state_wins_over_drift():
    """"The last attempt failed" is more actionable than "it changed since"."""
    decl = parse_declaration(_toml("polars"))
    stored = _manifest("stale", state=STATE_FAILED)
    assert manifest_state(decl, stored) == STATE_FAILED


def test_interpreter_mismatch_names_both_baselines():
    message = interpreter_mismatch(_manifest("x", python_version="3.11"),
                                  python_version="3.13")
    assert "3.11" in message and "3.13" in message


def test_manifest_without_interpreter_fields_is_a_mismatch():
    """Written before the field existed: reinstall beats an unreadable ImportError."""
    assert interpreter_mismatch(DepsManifest(state=STATE_READY, fingerprint="x"))


def test_matching_interpreter_reports_no_mismatch():
    assert interpreter_mismatch(_manifest("x")) == ""


# ── paths and task params ─────────────────────────────────────────────────


def test_overlay_paths_are_derived_from_the_caller_root():
    """The same copy is a host path to the runtime and a mount point inside a
    sandbox, so nothing here may be absolute or cached."""
    assert site_packages_path("/data/ws") == "/data/ws/.datacode/deps/site-packages"
    assert site_packages_path("/workspace") == "/workspace/.datacode/deps/site-packages"
    assert manifest_path("/workspace") == "/workspace/.datacode/deps/manifest.json"
    assert (
        constraints_path("/workspace")
        == "/workspace/.datacode/deps/platform-constraints.txt"
    )


def test_install_plan_round_trips_through_task_params():
    plan = InstallPlan(
        requirements=("polars>=1.0",),
        fingerprint="f",
        platform_packages=(("dagster", "1.13.21"), ("numpy", "1.26.4")),
        max_bytes=10,
    )
    assert InstallPlan.from_params(plan.to_params()) == plan


def test_a_plan_carries_the_baseline_the_installer_cannot_compute():
    """The installing interpreter is NOT the one that consumes the overlay."""
    plan = InstallPlan.from_params(
        {
            "requirements": ["polars>=1.0"],
            # A mapping as well as pairs: the runtime holds a dict.
            "platform_packages": {"Dagster": "1.13.21"},
        }
    )
    # Canonicalised on the way in, so the installer's lookups cannot miss on a
    # capitalisation difference.
    assert plan.baseline == {"dagster": "1.13.21"}


def test_a_plan_without_a_baseline_is_representable_so_the_runner_can_refuse():
    """Fail closed, not "the platform provides nothing"."""
    plan = InstallPlan.from_params({"requirements": ["polars"]})
    assert plan.baseline == {}


def test_install_plan_rejects_params_that_are_not_requirement_strings():
    with pytest.raises(DepsDeclarationError):
        InstallPlan.from_params({"requirements": [{"name": "polars"}]})


# ── transitive conflicts ──────────────────────────────────────────────────
#
# A declared package can be compatible with the baseline while its OWN
# dependencies are not. Nothing in the declaration mentions the blocking
# package, so no check of the file can find it — which is why this shape gets
# its own payload instead of being squeezed into PlatformConflict.


def test_relaxing_a_requirement_drops_only_its_ceilings():
    """The lower bound is a requirement; the upper one is usually a habit."""
    assert relax_upper_bounds("feast[postgres]<0.35.0,>=0.34.0") == (
        "feast[postgres]>=0.34.0"
    )
    # A tilde bound is a ceiling too, even though it does not look like one.
    assert relax_upper_bounds("numpy~=1.26") == "numpy"
    # An exclusion is not a ceiling: the user meant it, so it survives.
    assert relax_upper_bounds("numpy>=1.26,!=1.26.5") == "numpy!=1.26.5,>=1.26"


def test_a_requirement_without_a_ceiling_relaxes_to_itself():
    """The caller compares the two to know whether probing can change anything.

    Re-probing an already-open requirement would only repeat the failure it was
    meant to explain.
    """
    assert relax_upper_bounds("feast[postgres]>=0.34.0") == "feast[postgres]>=0.34.0"


def test_relaxing_keeps_the_marker():
    """A conditional requirement stays conditional."""
    assert (
        relax_upper_bounds('polars<1.0; python_version < "3.12"')
        == 'polars; python_version < "3.12"'
    )


def _report(*pairs: tuple[str, str]) -> list[dict]:
    """A pip report whose entries state no dependencies of their own."""
    return [{"metadata": {"name": name, "version": version}} for name, version in pairs]


def _report_with_requires(
    *entries: tuple[str, str, tuple[str, ...]],
) -> list[dict]:
    """A pip report in the shape pip actually produces.

    ``metadata`` is PEP 566 core metadata, so it carries ``requires_dist`` —
    which is where the RANGE a dependency states lives, and the only evidence
    that distinguishes a real blocker from a package that merely has a newer
    release on the index.
    """
    return [
        {
            "metadata": {
                "name": name,
                "version": version,
                "requires_dist": list(requires),
            }
        }
        for name, version, requires in entries
    ]


def test_a_blocker_is_a_range_the_platform_falls_outside_of():
    """THE case: feast needs tenacity<9, the platform pins tenacity 9.1.4."""
    baseline = {"tenacity": "9.1.4", "numpy": "2.5.2", "pandas": "2.3.3"}
    blockers = blockers_from_resolution(
        _report_with_requires(
            ("feast", "0.65.0", ("tenacity<9,>=7", "numpy>=2.0.0", "pandas>=1.4.3")),
            ("tenacity", "8.5.0", ()),
            ("numpy", "2.5.3", ()),
            ("pandas", "2.3.3", ()),
        ),
        baseline,
    )

    # Exactly one, and it is the one pip refused over. numpy is NOT a blocker:
    # the resolution floated it to 2.5.3 because that is the newest release, but
    # feast asked for `>=2.0.0` and the platform's 2.5.2 satisfies it. Reporting
    # it produced a list of ten blockers for a single real conflict, and a
    # verdict of "no version fits this platform" that was simply untrue.
    assert [item["name"] for item in blockers] == ["tenacity"]
    assert blockers[0] == {
        "name": "tenacity",
        "required": "<9,>=7",
        "platform_version": "9.1.4",
        # The declaration never mentions tenacity, so the package that does has
        # to be named or the report is unactionable.
        "required_by": "feast",
    }


def test_a_newer_release_on_the_index_is_not_a_blocker():
    """The negative control for the bug the range-based rule fixed."""
    assert (
        blockers_from_resolution(
            _report_with_requires(
                ("feast", "0.65.0", ("numpy>=2.0.0", "protobuf>=4.24.0")),
                ("numpy", "2.5.3", ()),
                ("protobuf", "7.36.1", ()),
            ),
            {"numpy": "2.5.2", "protobuf": "6.33.6"},
        )
        == ()
    )


def test_a_dependency_that_was_not_installed_is_ignored():
    """Extra- and marker-gated requirements must not become blockers.

    The report does not say which extras were requested, so "did the resolver
    actually install it" is the only sound filter: a requirement whose package
    is absent from the resolution did not apply.
    """
    assert (
        blockers_from_resolution(
            _report_with_requires(("feast", "0.65.0", ("tenacity<9; extra == 'aws'",))),
            {"tenacity": "9.1.4"},
        )
        == ()
    )


def test_without_dependency_metadata_only_a_backwards_move_is_evidence():
    """A pip too old to report requires_dist still has to say something.

    A resolution that moved a baseline package BACKWARDS can only have done so
    because something stated a ceiling; a forward move is just the index having
    a newer release.
    """
    blockers = blockers_from_resolution(
        _report(("feast", "0.34.1"), ("numpy", "1.24.3"), ("tqdm", "4.70.1")),
        {"numpy": "2.5.2", "tqdm": "4.67.3"},
    )
    assert [item["name"] for item in blockers] == ["numpy"]
    assert blockers[0]["required"] == "<=1.24.3"


def test_blockers_survive_a_malformed_report():
    """The report is pip's output; a diagnosis must not crash on its shape."""
    assert blockers_from_resolution([{}, {"metadata": None}, "junk"], {"a": "1"}) == ()


def test_a_transitive_conflict_names_the_blocking_package_and_the_edit():
    conflict = TransitiveConflict(
        requirement="feast[postgres]<0.66.0,>=0.60.0",
        name="feast",
        blockers=(
            {
                "name": "tenacity",
                "required": "<9,>=7",
                "platform_version": "9.1.4",
                "required_by": "feast",
            },
        ),
        suggested="feast[postgres]>=0.60.0",
        resolved_version="0.66.1",
    )

    message = conflict.message()
    assert "feast[postgres]<0.66.0,>=0.60.0" in message
    # The package the user never declared, its range and the platform's version.
    assert "tenacity <9,>=7" in message and "tenacity==9.1.4" in message
    # The edit, verified by a resolve rather than guessed.
    assert "feast[postgres]>=0.60.0" in message and "0.66.1" in message
    assert conflict.fixable


def test_an_unfixable_transitive_conflict_says_so_instead_of_guessing():
    """'No release fits' is an answer — an operator's problem, not a puzzle."""
    conflict = TransitiveConflict(
        requirement="feast[postgres]<0.66.0,>=0.60.0",
        name="feast",
        blockers=(
            {"name": "numpy", "required": "<1.25", "platform_version": "2.5.2"},
        ),
    )
    assert not conflict.fixable
    assert "operator" in conflict.message()


def test_a_dead_end_names_the_pin_that_cannot_move():
    """With yields in play, "no fix" needs a reason or it looks arbitrary.

    Every yieldable pin was already released and retried before this verdict,
    so the remaining blocker is the one thing an operator can act on — and the
    name is what the user quotes when asking them.
    """
    conflict = TransitiveConflict(
        requirement="feast",
        name="feast",
        blockers=(
            {"name": "numpy", "required": "<1.25", "platform_version": "2.5.2"},
        ),
        protected=("numpy",),
    )
    message = conflict.message()
    assert "numpy" in message
    assert "compiled extension" in message
    assert "operator" in message


def test_a_transitive_conflict_round_trips_through_the_wire():
    conflict = TransitiveConflict(
        requirement="feast[postgres]<0.66.0,>=0.60.0",
        name="feast",
        blockers=(
            {
                "name": "tenacity",
                "required": "<9,>=7",
                "platform_version": "9.1.4",
                "required_by": "feast",
            },
        ),
        suggested="feast[postgres]>=0.60.0",
        resolved_version="0.66.1",
        protected=("tenacity",),
    )
    assert TransitiveConflict.from_dict(conflict.to_dict()) == conflict


def test_a_transitive_conflict_from_another_build_is_dropped_not_fatal():
    """It crosses an image boundary; a record this build cannot read is junk.

    The prose message reaches the user regardless, so dropping the structure
    loses an action, never the explanation.
    """
    parsed = TransitiveConflict.from_dict(
        {"requirement": "feast", "name": "feast", "blockers": ["junk", {"name": "numpy"}]}
    )
    assert parsed.blockers == (
        {
            "name": "numpy",
            "required": "",
            "platform_version": "",
            "required_by": "",
        },
    )
    assert parsed.protected == ()


# ── layering the baseline: which pins may yield ───────────────────────────


def test_the_yieldable_set_withholds_compiled_and_platform_packages():
    baseline = {
        "tenacity": "9.1.4",
        "tqdm": "4.67.3",
        "numpy": "2.5.2",
        "dagster": "1.13.21",
        "pydantic": "2.13.4",
    }

    yieldable = relaxable_baseline(baseline, compiled=("numpy",))

    # Pure-Python leaves may yield; the ABI-bound one and the platform's own
    # machinery may not — the whole point of the layering.
    assert yieldable == frozenset({"tenacity", "tqdm"})


def test_an_operator_can_withhold_and_can_re_admit():
    baseline = {"tenacity": "9.1.4", "tqdm": "4.67.3", "numpy": "2.5.2"}

    assert relaxable_baseline(
        baseline, compiled=("numpy",), protected_extra=("tqdm",)
    ) == frozenset({"tenacity"})
    # Re-admission is applied LAST, so a deployment that knows better than the
    # derived default can say so — including for a compiled package.
    assert relaxable_baseline(
        baseline,
        compiled=("numpy",),
        protected_extra=("tqdm",),
        relaxable_extra=("tqdm", "numpy"),
    ) == frozenset({"tenacity", "tqdm", "numpy"})


def test_a_distribution_this_interpreter_cannot_inspect_counts_as_compiled():
    """Fail closed: an unreadable distribution must not become yieldable."""
    assert compiled_distributions(("no-such-distribution-anywhere",)) == frozenset(
        {"no-such-distribution-anywhere"}
    )


def test_compiled_detection_finds_a_real_extension_module():
    """Asserted against the real interpreter, not a fixture.

    The rule is only as good as what it sees in an actual installed tree, and
    `packaging` (pure Python) versus a wheel with a `.so` is the distinction the
    whole layering rests on.
    """
    found = compiled_distributions(("packaging", "pydantic-core"))
    assert "packaging" not in found
    # pydantic-core is a Rust extension; it ships in every environment that has
    # pydantic, which every service here does.
    assert "pydantic-core" in found


def test_yielded_pins_are_left_out_of_the_constraints_file():
    text = baseline_constraints(
        {"tenacity": "9.1.4", "numpy": "2.5.2"}, exclude=("tenacity",)
    )
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == ["numpy==2.5.2"]
    # Named in the header: the file is the record of what pip was given, and an
    # omission nobody can see is the kind of thing that gets debugged twice.
    assert "tenacity" in text


def test_a_yielded_package_in_the_overlay_is_not_a_violation():
    """Otherwise the yield mechanism would fail its own post-install check."""
    overlay = {"tenacity": "8.5.0", "feast": "0.65.0"}
    baseline = {"tenacity": "9.1.4"}

    assert detect_shadowed(overlay, baseline, allowed=("tenacity",)) == ((), ())
    # Without the yield it is exactly what it looks like: a shadow.
    shadowed, _ = detect_shadowed(overlay, baseline)
    assert [item["name"] for item in shadowed] == ["tenacity"]


# ── reading pip's conflict report ─────────────────────────────────────────

_REAL_CONFLICT = """ERROR: Cannot install feast because these package versions
have conflicting dependencies.

The conflict is caused by:
    feast 0.65.0 depends on tenacity<9 and >=7
    feast 0.64.0 depends on tenacity<9 and >=7
    the platform pins tenacity==9.1.4 (from platform-constraints.txt)
"""


def test_the_implicated_pins_come_from_pips_own_report():
    """pip knows which of several hundred pins collided; asking again is waste."""
    implicated = pins_implicated_in_conflict(
        _REAL_CONFLICT, {"tenacity": "9.1.4", "numpy": "2.5.2"}
    )

    assert set(implicated) == {"tenacity"}
    # The dependency side names who stated the bound, which the constraint
    # citation does not.
    assert implicated["tenacity"] == "feast"


def test_both_spellings_of_the_constraint_citation_are_recognised():
    """The transcript rewrites pip's sentence, and this reads the transcript."""
    raw = "    The user requested (constraint) tenacity==9.1.4\n"
    assert pins_implicated_in_conflict(raw, {"tenacity": "9.1.4"}) == {"tenacity": ""}


def test_a_declared_package_named_in_the_report_is_not_a_platform_pin():
    """The filter that keeps a yield from being offered for the wrong package."""
    raw = "    feast 0.65.0 depends on pyarrow>=16.1.0\n"
    assert pins_implicated_in_conflict(raw, {"tenacity": "9.1.4"}) == {}
