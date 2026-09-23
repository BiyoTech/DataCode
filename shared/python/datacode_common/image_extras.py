"""Build-time assertion that an image's opt-in extras actually landed.

``pip install "./svc[a,b]"`` treats an UNKNOWN extra as a warning: it installs
everything it did recognise and exits 0. That is the wrong failure mode for the
one job these extras have — putting a package into the platform baseline. A
misspelled group, or a shell fragment pasted into the build-arg, produces an
image that looks healthy, quietly omits the packages, and surfaces much later as
a ``deps_no_wheel`` refusal for the very package the build was meant to provide
(``pyspark`` publishes only a source distribution, so declaring it in a working
copy can never be the fix — see workspace-runtime/src/deps/).

Both images call this right after their install step:

    python -m datacode_common.image_extras <pyproject> <ARG_NAME> "<value>"

Two things are checked, because they fail differently:

* every name is a real extra of that ``pyproject.toml`` — catches typos and
  pasted command lines before anything else runs;
* every requirement in the group resolves to an installed distribution —
  catches a group that exists but whose install was skipped or rolled back.

An empty value is success: extras are opt-in, and an ordinary build asks for
none.
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys
import tomllib
from pathlib import Path

__all__ = ["main", "parse_extras", "verify_extras"]


def parse_extras(value: str) -> list[str]:
    """Split a build-arg into extra names.

    Commas are the documented separator. Whitespace is treated as a separator
    too, so a pasted shell fragment (``"spark make build up"``) is reported as
    the unknown names it contains rather than being silently accepted as one
    exotic name.
    """
    return [token for token in value.replace(",", " ").split() if token]


def verify_extras(pyproject: Path, arg_name: str, value: str) -> list[str]:
    """Return human-readable problems with ``value``; empty means it is fine."""
    names = parse_extras(value)
    if not names:
        return []

    with pyproject.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    groups = project.get("optional-dependencies", {})
    known = ", ".join(sorted(groups)) or "(none declared)"

    problems: list[str] = []
    for name in names:
        if name not in groups:
            problems.append(
                f"{arg_name} names {name!r}, which is not an extra of "
                f"{project.get('name', pyproject.parent.name)}. Known extras: {known}. "
                f"Pass ONLY comma-separated group names, e.g. "
                f"--build-arg {arg_name}=spark"
            )
            continue
        for requirement in groups[name]:
            # The distribution name is everything before the first version
            # specifier, extra marker, or environment marker.
            distribution = requirement
            for separator in ("[", ";", "=", ">", "<", "!", "~", " "):
                distribution = distribution.split(separator, 1)[0]
            distribution = distribution.strip()
            if not distribution:
                continue
            try:
                metadata.version(distribution)
            except metadata.PackageNotFoundError:
                problems.append(
                    f"extra {name!r} was requested but {distribution!r} is not "
                    f"installed — the install did not apply it"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(
            "usage: python -m datacode_common.image_extras "
            "<pyproject.toml> <ARG_NAME> <value>",
            file=sys.stderr,
        )
        return 2

    pyproject, arg_name, value = Path(args[0]), args[1], args[2]
    if not pyproject.is_file():
        print(f"image_extras: no such file: {pyproject}", file=sys.stderr)
        return 2

    problems = verify_extras(pyproject, arg_name, value)
    if problems:
        for problem in problems:
            print(f"image_extras: {problem}", file=sys.stderr)
        return 1

    names = parse_extras(value)
    if names:
        print(f"image_extras: {arg_name} satisfied: {', '.join(names)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
