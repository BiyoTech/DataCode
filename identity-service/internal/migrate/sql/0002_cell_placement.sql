-- 0002: cell placement + user preferences (design 03 §2.4, design 05 §3).
--
-- `tenant_placement` / `cells` are the GLOBAL routing table: which cell serves
-- a tenant, and the state of an in-flight migration. Explicit mapping rather
-- than consistent hashing (ADR-006): tenant sizes differ by orders of
-- magnitude, dedicated cells must be expressible, and a migration has to be
-- controllable one tenant at a time.
--
-- users.timezone / users.locale implement the platform rule that the backend
-- stores UTC and the FRONTEND renders in the user's zone: the preference has
-- to live with the user, globally, so it is identical in every cell.

CREATE TABLE IF NOT EXISTS cells (
    cell_id           VARCHAR(32)  NOT NULL PRIMARY KEY,
    region            VARCHAR(32)  NOT NULL DEFAULT '',
    -- Cell entry point the global edge routes to.
    endpoint          VARCHAR(255) NOT NULL DEFAULT '',
    -- active | draining | maintenance
    status            VARCHAR(24)  NOT NULL DEFAULT 'active',
    capacity_tenants  INTEGER      NOT NULL DEFAULT 200,
    assigned_tenants  INTEGER      NOT NULL DEFAULT 0,
    created_at        VARCHAR(64)  NOT NULL,
    updated_at        VARCHAR(64)  NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_placement (
    tenant_id       VARCHAR(64) NOT NULL PRIMARY KEY,
    cell_id         VARCHAR(32) NOT NULL,
    -- Set while a migration is staged; empty otherwise.
    target_cell_id  VARCHAR(32) NOT NULL DEFAULT '',
    -- stable | draining | migrating | verifying
    migration_state VARCHAR(24) NOT NULL DEFAULT 'stable',
    created_at      VARCHAR(64) NOT NULL,
    updated_at      VARCHAR(64) NOT NULL
);

-- IANA zone name (e.g. Asia/Shanghai). Default UTC so an unset preference is
-- unambiguous rather than server-local.
-- Guarded: only PostgreSQL has ADD COLUMN IF NOT EXISTS, so the migrator
-- probes the catalog through dbx instead (see migrate.Statement).
-- +migrate ifNoColumn users.timezone
ALTER TABLE users ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'UTC';

-- UI language: 'zh' | 'en'.
-- +migrate ifNoColumn users.locale
ALTER TABLE users ADD COLUMN locale VARCHAR(16) NOT NULL DEFAULT 'en';

-- Bumped whenever a tenant's roles or bindings change, so a gateway holding a
-- cached authorization snapshot can detect staleness without polling. Carried
-- in the JWT as `perms_version`.
CREATE TABLE IF NOT EXISTS tenant_perms_version (
    tenant_id  VARCHAR(64) NOT NULL PRIMARY KEY,
    version    INTEGER     NOT NULL DEFAULT 1,
    updated_at VARCHAR(64) NOT NULL
);

-- Seed the default cell so a single-cell deployment (M0/M1) needs no manual
-- setup: every tenant without an explicit placement row resolves to it.
-- `SELECT ... WHERE NOT EXISTS` rather than a dialect-specific upsert
-- (ON CONFLICT / ON DUPLICATE KEY differ across the three backends), and
-- MySQL requires a FROM for a bare SELECT, hence the dual-free subquery form.
INSERT INTO cells (cell_id, region, endpoint, status, capacity_tenants, assigned_tenants, created_at, updated_at)
SELECT 'cell-0', '', '', 'active', 200, 0, '1970-01-01T00:00:00Z', '1970-01-01T00:00:00Z'
  FROM (SELECT 1) AS seed
 WHERE NOT EXISTS (SELECT 1 FROM cells WHERE cell_id = 'cell-0');
