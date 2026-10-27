# THE INSTALL LIST for this working copy's third-party Python packages.
#
# `[project].dependencies` below is the ONE place to declare what your assets
# import. It is shared by everyone working on this workspace (it travels with the
# repository and with the branch), and the platform installs it for the whole
# working copy — there is no per-user environment.
#
# How to add a package:
#   1. add it here with a version range, e.g. "polars>=1.0,<2";
#   2. press Reload in the asset page, or run `dc deps sync`.
#
# What the platform does with it:
#   * installs into .datacode/deps/site-packages (regenerable, git-ignored) and
#     puts that directory first on PYTHONPATH for the code location, for sandbox
#     commands and for your Python models;
#   * skips anything the platform itself already provides (see below) and
#     REFUSES the sync if you ask for a version of one of those that the
#     platform does not ship — an overlay always shadows the platform's copy, so
#     it cannot be honoured without breaking the code location;
#   * pins the resolver to the platform's versions, so a package you declare
#     cannot drag in a different numpy/pyarrow than the one your code location
#     runs on;
#   * installs pre-built wheels only: there is no compiler in the install sandbox.
#
# To see everything the platform provides, with the exact string to declare for
# each: `dc deps baseline` (or `--all` for the full list).
#
# `pip install` is disabled on purpose: a package installed by hand disappears
# with the sandbox that ran the command, and nothing records why it was there.
#
# [tool.dagster].module_name below is what Dagster reads to find the entrypoint.

[project]
name = "asset-dag"
version = "0.1.0"
description = "Data+AI pipeline (Dagster + Daft) for workspace {{ workspace_name }}"
requires-python = ">={{ python_version }}"
dependencies = [
    # Provided by the platform image — these lines were GENERATED from the
    # baseline of the image that created this working copy, so they name
    # versions that really are installed. Declaring them costs nothing: the sync
    # recognises them as platform-provided and installs nothing.
{{ platform_dependencies }}
    # Add yours below.
]

[tool.dagster]
module_name = "definitions"
