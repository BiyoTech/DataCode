-- 0001: identity baseline.
--
-- Equivalent to the DDL the gateway used to run ad-hoc via
-- `CREATE TABLE IF NOT EXISTS` (gateway/internal/auth/store.go). Keeping it
-- IF NOT EXISTS makes this migration adoptable on an EXISTING database: an
-- environment upgraded from the pre-P2 gateway already has these tables, so
-- 0001 becomes a no-op there and only records itself in the ledger.
--
-- Dialect-neutral on purpose (design 05 §4.2): VARCHAR/TEXT columns only, no
-- auto-increment, timestamps stored as UTC RFC3339 strings. One statement set
-- is therefore valid on sqlite, mysql and postgresql alike.

CREATE TABLE IF NOT EXISTS users (
    id             VARCHAR(64) NOT NULL PRIMARY KEY,
    username       VARCHAR(64) NOT NULL,
    password_hash  TEXT NOT NULL,
    status         VARCHAR(32) NOT NULL DEFAULT 'active',
    last_tenant_id VARCHAR(64) NOT NULL DEFAULT '',
    created_at     VARCHAR(64) NOT NULL,
    updated_at     VARCHAR(64) NOT NULL,
    UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS tenants (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_members (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    tenant_id  VARCHAR(64) NOT NULL,
    user_id    VARCHAR(64) NOT NULL,
    role       VARCHAR(32) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    user_id    VARCHAR(64) NOT NULL,
    tenant_id  VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    revoked_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    UNIQUE (token_hash)
);

-- Global permission catalog. Rows are synchronized from the code-side catalog
-- on startup and are never mutated through the API.
CREATE TABLE IF NOT EXISTS permissions (
    code        VARCHAR(96)  NOT NULL PRIMARY KEY,
    scope       VARCHAR(32)  NOT NULL,
    resource    VARCHAR(64)  NOT NULL,
    action      VARCHAR(32)  NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    deprecated  VARCHAR(8)   NOT NULL DEFAULT '0',
    created_at  VARCHAR(64)  NOT NULL,
    updated_at  VARCHAR(64)  NOT NULL
);

-- Roles. tenant_id = '' marks a SYSTEM TEMPLATE written only by the seeder;
-- tenant_id = <tid> marks a tenant-owned instance the seeder never touches.
-- That separation is what makes a built-in role's permission set
-- tenant-editable without being reverted on the next startup.
CREATE TABLE IF NOT EXISTS roles (
    id          VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id   VARCHAR(64)  NOT NULL DEFAULT '',
    scope       VARCHAR(32)  NOT NULL,
    code        VARCHAR(64)  NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    is_builtin  VARCHAR(8)   NOT NULL DEFAULT '0',
    created_at  VARCHAR(64)  NOT NULL,
    updated_at  VARCHAR(64)  NOT NULL,
    UNIQUE (tenant_id, scope, code)
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         VARCHAR(64) NOT NULL,
    permission_code VARCHAR(96) NOT NULL,
    created_at      VARCHAR(64) NOT NULL,
    PRIMARY KEY (role_id, permission_code)
);

-- Workspace role bindings. tenant_id is carried explicitly so cross-tenant
-- checks never need a join.
CREATE TABLE IF NOT EXISTS workspace_members (
    id             VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id      VARCHAR(64)  NOT NULL,
    workspace_id   VARCHAR(128) NOT NULL,
    principal_type VARCHAR(32)  NOT NULL DEFAULT 'user',
    user_id        VARCHAR(64)  NOT NULL,
    role_code      VARCHAR(64)  NOT NULL,
    granted_by     VARCHAR(64)  NOT NULL DEFAULT '',
    created_at     VARCHAR(64)  NOT NULL,
    updated_at     VARCHAR(64)  NOT NULL,
    UNIQUE (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS rbac_audit_log (
    id         VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL,
    actor_id   VARCHAR(64)  NOT NULL,
    action     VARCHAR(64)  NOT NULL,
    scope_type VARCHAR(32)  NOT NULL,
    scope_id   VARCHAR(128) NOT NULL DEFAULT '',
    target_id  VARCHAR(128) NOT NULL DEFAULT '',
    detail     VARCHAR(512) NOT NULL DEFAULT '',
    created_at VARCHAR(64)  NOT NULL
);
