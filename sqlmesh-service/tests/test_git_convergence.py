"""Tests for the Git concept convergence delivered in Phase 3.

Phase 3 makes the Environment the only development-line concept a user faces.
Two invariants carry that guarantee, and both are checked here:

1. the branch lifecycle endpoints and primitives are GONE, so a branch outside
   the registry cannot be produced through the platform at all;
2. ``GET /branches`` reports WHICH environment each branch backs, which is what
   lets the UI show branches as read-only labels instead of actionable objects.

The web layer is asserted at SOURCE level rather than by importing it: the API
modules pull in FastAPI/httpx, which turns a behavioural guarantee into a test
that only runs when the full serving stack is installed. The naming algebra —
the actual logic under test — is imported normally.
"""

import ast
import pathlib

import pytest

from src.environments import naming

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _route_decorators(module_path: pathlib.Path) -> set[tuple[str, str]]:
    """Collect ``(http_method, path)`` from ``@router.<verb>("<path>")``.

    Parsing beats a substring search: it ignores the same text appearing in a
    docstring or a comment, so a documented-but-removed endpoint cannot make the
    test fail, and a commented-out one cannot make it pass.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    routes: set[tuple[str, str]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute):
                continue
            owner = func.value
            if not (isinstance(owner, ast.Name) and owner.id == "router"):
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            routes.add((func.attr.upper(), str(decorator.args[0].value)))
    return routes


def _defines(module_path: pathlib.Path, name: str) -> bool:
    """Whether a module defines a top-level class, function or assignment."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return True
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
    return False


def _class_attributes(module_path: pathlib.Path, class_name: str) -> set[str]:
    """Names defined inside a class body (methods, fields, nested classes)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names: set[str] = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(item.name)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    names.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    names.update(
                        t.id for t in item.targets if isinstance(t, ast.Name)
                    )
            return names
    return set()


class TestBranchLifecycleRemoved:
    """Nothing in the platform may create, switch or delete a branch outside the
    environment registry."""

    def test_no_branch_write_endpoints(self):
        routes = _route_decorators(_SRC / "api" / "rest" / "git.py")
        forbidden = {("POST", "/checkout"), ("POST", "/branch/delete"), ("POST", "/branch")}
        assert not (routes & forbidden), (
            "branch lifecycle belongs to the environment registry, but the Git "
            f"API still exposes {sorted(routes & forbidden)}"
        )

    def test_branch_listing_is_read_only(self):
        """``/branches`` survives, but only as a read."""
        routes = _route_decorators(_SRC / "api" / "rest" / "git.py")
        branch_methods = {method for method, path in routes if path == "/branches"}
        assert branch_methods == {"GET"}

    def test_request_schemas_are_gone(self):
        """Removing the endpoint but keeping its schema invites it back."""
        requests_py = _SRC / "api" / "models" / "requests.py"
        assert not _defines(requests_py, "GitCheckoutRequest")
        assert not _defines(requests_py, "GitDeleteBranchRequest")

    def test_checkout_primitive_is_gone(self):
        """Switching environment opens another worktree, so no code path may move
        a working copy to a different branch."""
        attributes = _class_attributes(_SRC / "workspace" / "git_ops.py", "GitOperations")
        assert attributes, "GitOperations must still exist"
        for removed in ("checkout", "fetch_and_checkout", "fetch_and_checkout_async"):
            assert removed not in attributes, (
                f"GitOperations.{removed} would let a caller move a worktree to "
                "another branch, breaking the Environment <-> Branch 1:1 binding"
            )

    def test_worktree_primitives_are_kept(self):
        """The replacement mechanism must be present: environments are added as
        worktrees, never checked out."""
        attributes = _class_attributes(
            _SRC / "workspace" / "git_ops.py", "GitWorktreeOperations"
        )
        assert {"worktree_add", "worktree_remove", "worktree_list"} <= attributes


class TestBranchEnvironmentAnnotation:
    """Every branch is annotated with the environment it backs, or as external."""

    @pytest.mark.parametrize(
        "branch,expected_env",
        [
            ("main", "prod"),
            ("env/dev_alice", "dev_alice"),
            ("env/feature_orders", "feature_orders"),
        ],
    )
    def test_managed_branches_resolve_to_an_environment(self, branch, expected_env):
        assert naming.env_of(branch) == expected_env

    @pytest.mark.parametrize(
        "branch",
        [
            "release/1.0",     # brought in from an external repository
            "feature/orders",  # outside the platform namespace
            "env/Bad-Name",    # inside the namespace but not a legal env name
            "env/",            # empty remainder
            "",
        ],
    )
    def test_external_branches_are_not_claimed(self, branch):
        """An unclaimed branch resolves to ``None``, which is what marks it
        read-only: it cannot be planned, applied or used as a base until it is
        adopted as an environment."""
        assert naming.env_of(branch) is None

    def test_annotation_round_trips(self):
        """``branch_of`` and ``env_of`` are inverses for every legal name, so a
        branch label in the UI can never disagree with the registry."""
        for env in ("prod", "dev_alice", "feature_orders", "s1"):
            assert naming.env_of(naming.branch_of(env)) == env

    def test_remote_ref_prefix_must_be_stripped(self):
        """Remote refs carry the remote name, which is not part of the branch
        identity the naming algebra works on. Without stripping it, every remote
        branch would be reported as external."""
        remote_ref = "origin/env/dev_alice"
        assert naming.env_of(remote_ref) is None
        assert naming.env_of(remote_ref.split("/", 1)[1]) == "dev_alice"

    def test_production_branch_is_not_namespaced(self):
        """Production maps to ``main``: the branch users already have, so
        adopting an existing repository needs no rename."""
        assert naming.branch_of(naming.PRODUCTION_ENV) == naming.PRODUCTION_BRANCH
        assert not naming.PRODUCTION_BRANCH.startswith(naming.BRANCH_NAMESPACE)


class TestWorkspaceOpenSelectsEnvironment:
    """Opening or pushing a working copy addresses an ENVIRONMENT, not a branch."""

    def test_open_request_replaced_branch_with_environment(self):
        requests_py = _SRC / "api" / "models" / "requests.py"
        fields = _class_attributes(requests_py, "OpenWorkspaceRequest")
        assert "environment" in fields
        assert "branch" not in fields

    def test_sync_request_carries_the_environment(self):
        """Each environment is a separate worktree, so a push must state which
        one it commits instead of silently targeting the default."""
        fields = _class_attributes(
            _SRC / "api" / "models" / "requests.py", "SyncWorkspaceRequest"
        )
        assert "environment" in fields
