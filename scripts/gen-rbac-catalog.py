#!/usr/bin/env python3
"""Generate the language-side permission catalogs from shared/rbac-catalog.json.

The JSON file is the single declaration of which permission codes exist and how
the built-in role templates are composed. Two constant tables are generated from
it:

  * shared/go/rbac/catalog_gen.go            (gateway + identity-service)
  * shared/python/datacode_common/rbac/_catalog_data.py  (control + runtime)

Why generate instead of reading the JSON at runtime:

  * `//go:embed` cannot reference a parent directory, so the Go package next to
    the JSON still could not embed it;
  * `datacode_common` ships as a wheel whose payload is the package directory
    only, so a repository-relative JSON is simply absent in a deployed service.

Generated tables are therefore compile-time / import-time constants with zero
runtime IO, and BOTH languages additionally assert their table against the JSON
in their own test suite. A drift fails the tests rather than surfacing in
production as a permission code nobody can ever hold.

Usage:
    python3 scripts/gen-rbac-catalog.py            # regenerate both files
    python3 scripts/gen-rbac-catalog.py --check     # fail if either has drifted
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_JSON = REPO_ROOT / "shared" / "rbac-catalog.json"
GO_TARGET = REPO_ROOT / "shared" / "go" / "rbac" / "catalog_gen.go"
PY_TARGET = REPO_ROOT / "shared" / "python" / "datacode_common" / "rbac" / "_catalog_data.py"

# Marks every permission of a scope, keeping "a new workspace permission is
# automatically part of the owner role" true without restating the list.
WILDCARD_PREFIX = "*"

GENERATED_BY = "scripts/gen-rbac-catalog.py"
SOURCE_REF = "shared/rbac-catalog.json"


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


class CatalogError(RuntimeError):
    """The source file violates a contract the generated tables rely on."""


def load_catalog() -> dict:
    """Read and validate the source catalog.

    Validation happens here rather than in the tests because a malformed source
    would otherwise produce code that does not compile, with an error pointing
    at the generated file instead of the declaration.
    """
    raw = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))

    scopes = raw.get("scopes") or []
    permissions = raw.get("permissions") or []
    roles = raw.get("roles") or []
    if not scopes or not permissions or not roles:
        raise CatalogError("catalog must declare scopes, permissions and roles")

    seen_codes: set[str] = set()
    for perm in permissions:
        code = perm["code"]
        if code in seen_codes:
            raise CatalogError(f"duplicate permission code: {code}")
        seen_codes.add(code)
        if perm["scope"] not in scopes:
            raise CatalogError(f"permission {code} has unknown scope {perm['scope']}")
        # The code is the wire format of scope:resource:action; a mismatch would
        # make the role editor group a permission under the wrong resource.
        expected = f"{perm['scope']}:{perm['resource']}:{perm['action']}"
        short = f"{perm['scope']}:{perm['action']}"
        if code not in (expected, short):
            raise CatalogError(
                f"permission {code} does not match its scope/resource/action ({expected})"
            )

    by_code = {perm["code"]: perm for perm in permissions}
    seen_roles: set[tuple[str, str]] = set()
    for role in roles:
        key = (role["scope"], role["code"])
        if key in seen_roles:
            raise CatalogError(f"duplicate role: {role['scope']}/{role['code']}")
        seen_roles.add(key)
        if role["scope"] not in scopes:
            raise CatalogError(f"role {role['code']} has unknown scope {role['scope']}")
        granted = role["permissions"]
        if isinstance(granted, str):
            if granted != WILDCARD_PREFIX + role["scope"]:
                raise CatalogError(
                    f"role {role['code']} may only wildcard its own scope, got {granted}"
                )
            continue
        if not granted:
            raise CatalogError(f"role {role['code']} grants nothing")
        for code in granted:
            if code not in by_code:
                raise CatalogError(f"role {role['code']} references unknown permission {code}")
            if by_code[code]["scope"] != role["scope"]:
                raise CatalogError(
                    f"role {role['code']} ({role['scope']}) references {code} "
                    f"from scope {by_code[code]['scope']}"
                )
    return raw


# ---------------------------------------------------------------------------
# Identifier derivation
# ---------------------------------------------------------------------------


def _camel(text: str) -> str:
    """Turn one code segment into a Go identifier fragment."""
    return "".join(part.capitalize() for part in re.split(r"[_\-]", text) if part)


def go_permission_name(code: str) -> str:
    """`workspace:git:push` -> `PermWorkspaceGitPush`."""
    return "Perm" + "".join(_camel(segment) for segment in code.split(":"))


def go_role_name(scope: str, code: str) -> str:
    """`workspace` + `workspace_owner` -> `RoleWorkspaceOwner`.

    The scope is always part of the name: the code `viewer` exists in both
    scopes with different permission sets.
    """
    bare = code[len(scope) + 1 :] if code.startswith(f"{scope}_") else code
    return "Role" + _camel(scope) + _camel(bare)


def go_scope_name(scope: str) -> str:
    return "Scope" + _camel(scope)


def py_permission_name(code: str) -> str:
    """`workspace:git:push` -> `PERM_WORKSPACE_GIT_PUSH`."""
    return "PERM_" + code.replace(":", "_").replace("-", "_").upper()


def py_scope_name(scope: str) -> str:
    return "SCOPE_" + scope.upper()


# ---------------------------------------------------------------------------
# Go rendering
# ---------------------------------------------------------------------------


def _go_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _note_comment(text: str, indent: str, width: int = 76) -> list[str]:
    """Wrap a source-side note into comment lines."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) + len(indent) + 3 > width and current:
            lines.append(f"{indent}// {current}")
            current = word
        else:
            current = candidate
    if current:
        lines.append(f"{indent}// {current}")
    return lines


def render_go(catalog: dict) -> str:
    scopes: list[str] = catalog["scopes"]
    permissions: list[dict] = catalog["permissions"]
    roles: list[dict] = catalog["roles"]

    out: list[str] = [
        f"// Code generated from {SOURCE_REF} by {GENERATED_BY}. DO NOT EDIT.",
        "//",
        "// Edit the JSON declaration and regenerate. catalog_test.go asserts this",
        "// table against the same JSON, so a hand edit here fails the suite.",
        "",
        "package rbac",
        "",
        "// Authorization scopes. New scopes can be added without schema changes.",
        "const (",
    ]
    width = max(len(go_scope_name(scope)) for scope in scopes)
    for scope in scopes:
        name = go_scope_name(scope)
        out.append(f"\t{name.ljust(width)} Scope = {_go_string(scope)}")
    out += [
        ")",
        "",
        "// Scopes is every scope a permission or role may belong to.",
        "var Scopes = []Scope{" + ", ".join(go_scope_name(s) for s in scopes) + "}",
        "",
    ]

    # Permission codes, one const block per scope so the grouping stays readable.
    for scope in scopes:
        codes = [perm for perm in permissions if perm["scope"] == scope]
        if not codes:
            continue
        out += [f"// {scope.capitalize()}-scoped permission codes.", "const ("]
        width = max(len(go_permission_name(perm["code"])) for perm in codes)
        for perm in codes:
            name = go_permission_name(perm["code"])
            out.append(f"\t{name.ljust(width)} = {_go_string(perm['code'])}")
        out += [")", ""]

    out += [
        "// Built-in role codes. The literals are stable across versions because",
        "// they are persisted in tenant_members.role and workspace_members.role_code.",
        "const (",
    ]
    width = max(len(go_role_name(role["scope"], role["code"])) for role in roles)
    for role in roles:
        name = go_role_name(role["scope"], role["code"])
        out.append(f"\t{name.ljust(width)} = {_go_string(role['code'])}")
    out += [
        ")",
        "",
        "// Catalog is the authoritative list of permission codes known to the platform.",
        "// identity-service synchronizes it into the permissions table on startup; the",
        "// table is a projection and is never a source of truth.",
        "var Catalog = []PermissionDef{",
    ]
    for index, scope in enumerate(scopes):
        codes = [perm for perm in permissions if perm["scope"] == scope]
        if not codes:
            continue
        if index:
            out.append("")
        out.append(f"\t// {scope.capitalize()} scope.")
        for perm in codes:
            if perm.get("note"):
                out += _note_comment(perm["note"], "\t")
            out.append(
                "\t{{{code}, {scope}, {resource}, {action}, {description}}},".format(
                    code=go_permission_name(perm["code"]),
                    scope=go_scope_name(perm["scope"]),
                    resource=_go_string(perm["resource"]),
                    action=_go_string(perm["action"]),
                    description=_go_string(perm["description"]),
                )
            )
    out += [
        "}",
        "",
        "// BuiltinRoleTemplates are cloned into every tenant. Cloned rows are marked as",
        "// built-in: they cannot be deleted and their code cannot change, but their name",
        "// and permission set are editable by the tenant.",
        "var BuiltinRoleTemplates = []RoleTemplate{",
    ]
    for role in roles:
        out += [
            "\t{",
            f"\t\tCode:        {go_role_name(role['scope'], role['code'])},",
            f"\t\tScope:       {go_scope_name(role['scope'])},",
            f"\t\tName:        {_go_string(role['name'])},",
            f"\t\tDescription: {_go_string(role['description'])},",
        ]
        granted = role["permissions"]
        if isinstance(granted, str):
            scope = granted[len(WILDCARD_PREFIX) :]
            out.append(f"\t\tPermissions: PermissionsInScope({go_scope_name(scope)}),")
        else:
            out.append("\t\tPermissions: []string{")
            for code in granted:
                out.append(f"\t\t\t{go_permission_name(code)},")
            out.append("\t\t},")
        out.append("\t},")
    out += ["}", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Python rendering
# ---------------------------------------------------------------------------


def _py_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_python(catalog: dict) -> str:
    scopes: list[str] = catalog["scopes"]
    permissions: list[dict] = catalog["permissions"]

    out: list[str] = [
        f'"""Generated from {SOURCE_REF} by {GENERATED_BY}. DO NOT EDIT.',
        "",
        "Edit the JSON declaration and regenerate. tests/test_rbac_catalog.py asserts",
        "this table against the same JSON, so a hand edit here fails the suite.",
        "",
        "Role templates are deliberately NOT mirrored here: seeding roles is",
        "identity-service's job, and an unused copy would be one more thing to drift.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# Authorization scopes.",
    ]
    for scope in scopes:
        out.append(f"{py_scope_name(scope)} = {_py_string(scope)}")
    out += [
        "",
        "SCOPES: tuple[str, ...] = (",
    ]
    for scope in scopes:
        out.append(f"    {py_scope_name(scope)},")
    out += [")", ""]

    for scope in scopes:
        codes = [perm for perm in permissions if perm["scope"] == scope]
        if not codes:
            continue
        out.append(f"# {scope.capitalize()}-scoped permission codes.")
        for perm in codes:
            out.append(f"{py_permission_name(perm['code'])} = {_py_string(perm['code'])}")
        out.append("")

    out += [
        "# (code, scope, resource, action, description) rows, in declaration order.",
        "PERMISSION_ROWS: tuple[tuple[str, str, str, str, str], ...] = (",
    ]
    for perm in permissions:
        # One field per line: descriptions are prose and a single-line tuple
        # would exceed the repository's 100-column lint contract.
        out += [
            "    (",
            f"        {py_permission_name(perm['code'])},",
            f"        {py_scope_name(perm['scope'])},",
            f"        {_py_string(perm['resource'])},",
            f"        {_py_string(perm['action'])},",
            f"        {_py_string(perm['description'])},",
            "    ),",
        ]
    out += [")", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Compare content, not layout.

    The Go file is passed through gofmt when the toolchain is available, which
    changes alignment only. Comparing normalized text keeps `--check` usable in
    a Python-only lint job while still catching every semantic drift.
    """
    return re.sub(r"\s+", " ", text).strip()


def _gofmt(path: Path) -> None:
    """Format the generated Go file when the toolchain is available."""
    gofmt = shutil.which("gofmt")
    if gofmt is None:
        print("warning: gofmt not found, generated Go file left unformatted", file=sys.stderr)
        return
    subprocess.run([gofmt, "-w", str(path)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the generated files match the source instead of writing them",
    )
    args = parser.parse_args()

    try:
        catalog = load_catalog()
    except CatalogError as exc:
        print(f"invalid {SOURCE_REF}: {exc}", file=sys.stderr)
        return 1

    targets = [(GO_TARGET, render_go(catalog)), (PY_TARGET, render_python(catalog))]

    if args.check:
        drifted = []
        for path, expected in targets:
            actual = path.read_text(encoding="utf-8") if path.exists() else ""
            if _normalize(actual) != _normalize(expected):
                drifted.append(path.relative_to(REPO_ROOT))
        if drifted:
            print(
                "generated permission catalogs are stale: "
                + ", ".join(str(path) for path in drifted)
                + f"\nrun: python3 {GENERATED_BY}",
                file=sys.stderr,
            )
            return 1
        print(f"permission catalogs are in sync with {SOURCE_REF}")
        return 0

    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    _gofmt(GO_TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
