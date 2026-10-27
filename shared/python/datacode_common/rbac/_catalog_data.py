"""Generated from shared/rbac-catalog.json by scripts/gen-rbac-catalog.py. DO NOT EDIT.

Edit the JSON declaration and regenerate. tests/test_rbac_catalog.py asserts
this table against the same JSON, so a hand edit here fails the suite.

Role templates are deliberately NOT mirrored here: seeding roles is
identity-service's job, and an unused copy would be one more thing to drift.
"""

from __future__ import annotations

# Authorization scopes.
SCOPE_TENANT = "tenant"
SCOPE_WORKSPACE = "workspace"

SCOPES: tuple[str, ...] = (
    SCOPE_TENANT,
    SCOPE_WORKSPACE,
)

# Tenant-scoped permission codes.
PERM_TENANT_MEMBER_READ = "tenant:member:read"
PERM_TENANT_MEMBER_WRITE = "tenant:member:write"
PERM_TENANT_ROLE_READ = "tenant:role:read"
PERM_TENANT_ROLE_WRITE = "tenant:role:write"
PERM_TENANT_WORKSPACE_CREATE = "tenant:workspace:create"
PERM_TENANT_WORKSPACE_JOIN = "tenant:workspace:join"
PERM_TENANT_AGENT_USE = "tenant:agent:use"

# Workspace-scoped permission codes.
PERM_WORKSPACE_READ = "workspace:read"
PERM_WORKSPACE_DELETE = "workspace:delete"
PERM_WORKSPACE_MEMBER_READ = "workspace:member:read"
PERM_WORKSPACE_MEMBER_WRITE = "workspace:member:write"
PERM_WORKSPACE_GIT_BIND = "workspace:git:bind"
PERM_WORKSPACE_GIT_PUSH = "workspace:git:push"
PERM_WORKSPACE_ENV_READ = "workspace:env:read"
PERM_WORKSPACE_ENV_CREATE = "workspace:env:create"
PERM_WORKSPACE_ENV_DELETE = "workspace:env:delete"
PERM_WORKSPACE_ENV_PROMOTE = "workspace:env:promote"
PERM_WORKSPACE_CODE_READ = "workspace:code:read"
PERM_WORKSPACE_CODE_WRITE = "workspace:code:write"
PERM_WORKSPACE_PLAN_PREVIEW = "workspace:plan:preview"
PERM_WORKSPACE_PLAN_APPLY = "workspace:plan:apply"
PERM_WORKSPACE_QUERY_RUN = "workspace:query:run"
PERM_WORKSPACE_KNOWLEDGE_READ = "workspace:knowledge:read"
PERM_WORKSPACE_KNOWLEDGE_WRITE = "workspace:knowledge:write"
PERM_WORKSPACE_ONTOLOGY_READ = "workspace:ontology:read"
PERM_WORKSPACE_PIPELINE_READ = "workspace:pipeline:read"
PERM_WORKSPACE_PIPELINE_RUN = "workspace:pipeline:run"
PERM_WORKSPACE_LOGS_READ = "workspace:logs:read"

# (code, scope, resource, action, description) rows, in declaration order.
PERMISSION_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        PERM_TENANT_MEMBER_READ,
        SCOPE_TENANT,
        "member",
        "read",
        "View tenant members",
    ),
    (
        PERM_TENANT_MEMBER_WRITE,
        SCOPE_TENANT,
        "member",
        "write",
        "Add, update and remove tenant members",
    ),
    (
        PERM_TENANT_ROLE_READ,
        SCOPE_TENANT,
        "role",
        "read",
        "View roles and their permissions",
    ),
    (
        PERM_TENANT_ROLE_WRITE,
        SCOPE_TENANT,
        "role",
        "write",
        "Create, update and delete roles",
    ),
    (
        PERM_TENANT_WORKSPACE_CREATE,
        SCOPE_TENANT,
        "workspace",
        "create",
        "Create workspaces inside the tenant",
    ),
    (
        PERM_TENANT_WORKSPACE_JOIN,
        SCOPE_TENANT,
        "workspace",
        "join",
        "Can be granted access to workspaces",
    ),
    (
        PERM_TENANT_AGENT_USE,
        SCOPE_TENANT,
        "agent",
        "use",
        "Use agent sessions and language models",
    ),
    (
        PERM_WORKSPACE_READ,
        SCOPE_WORKSPACE,
        "workspace",
        "read",
        "Open the workspace and read its metadata",
    ),
    (
        PERM_WORKSPACE_DELETE,
        SCOPE_WORKSPACE,
        "workspace",
        "delete",
        "Delete the workspace",
    ),
    (
        PERM_WORKSPACE_MEMBER_READ,
        SCOPE_WORKSPACE,
        "member",
        "read",
        "View workspace members",
    ),
    (
        PERM_WORKSPACE_MEMBER_WRITE,
        SCOPE_WORKSPACE,
        "member",
        "write",
        "Grant and revoke workspace access",
    ),
    (
        PERM_WORKSPACE_GIT_BIND,
        SCOPE_WORKSPACE,
        "git",
        "bind",
        "Bind or rebind the Git repository",
    ),
    (
        PERM_WORKSPACE_GIT_PUSH,
        SCOPE_WORKSPACE,
        "git",
        "push",
        "Push commits to the remote repository",
    ),
    (
        PERM_WORKSPACE_ENV_READ,
        SCOPE_WORKSPACE,
        "env",
        "read",
        "View environments and their status",
    ),
    (
        PERM_WORKSPACE_ENV_CREATE,
        SCOPE_WORKSPACE,
        "env",
        "create",
        "Create environments, including their Git branch",
    ),
    (
        PERM_WORKSPACE_ENV_DELETE,
        SCOPE_WORKSPACE,
        "env",
        "delete",
        "Destroy non-production environments",
    ),
    (
        PERM_WORKSPACE_ENV_PROMOTE,
        SCOPE_WORKSPACE,
        "env",
        "promote",
        "Merge into the production branch and enable branch schedules",
    ),
    (
        PERM_WORKSPACE_CODE_READ,
        SCOPE_WORKSPACE,
        "code",
        "read",
        "Read models, files and lineage",
    ),
    (
        PERM_WORKSPACE_CODE_WRITE,
        SCOPE_WORKSPACE,
        "code",
        "write",
        "Create, modify and delete files",
    ),
    (
        PERM_WORKSPACE_PLAN_PREVIEW,
        SCOPE_WORKSPACE,
        "plan",
        "preview",
        "Run plan previews without applying",
    ),
    (
        PERM_WORKSPACE_PLAN_APPLY,
        SCOPE_WORKSPACE,
        "plan",
        "apply",
        "Apply plans to an environment",
    ),
    (
        PERM_WORKSPACE_QUERY_RUN,
        SCOPE_WORKSPACE,
        "query",
        "run",
        "Execute ad-hoc queries",
    ),
    (
        PERM_WORKSPACE_KNOWLEDGE_READ,
        SCOPE_WORKSPACE,
        "knowledge",
        "read",
        "Read business knowledge documents",
    ),
    (
        PERM_WORKSPACE_KNOWLEDGE_WRITE,
        SCOPE_WORKSPACE,
        "knowledge",
        "write",
        "Create and delete knowledge documents",
    ),
    (
        PERM_WORKSPACE_ONTOLOGY_READ,
        SCOPE_WORKSPACE,
        "ontology",
        "read",
        "Read the semantic ontology",
    ),
    (
        PERM_WORKSPACE_PIPELINE_READ,
        SCOPE_WORKSPACE,
        "pipeline",
        "read",
        "View pipelines, assets and runs",
    ),
    (
        PERM_WORKSPACE_PIPELINE_RUN,
        SCOPE_WORKSPACE,
        "pipeline",
        "run",
        "Trigger and cancel pipeline runs",
    ),
    (
        PERM_WORKSPACE_LOGS_READ,
        SCOPE_WORKSPACE,
        "logs",
        "read",
        "Read workspace service logs and the error archive",
    ),
)
