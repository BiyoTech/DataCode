package auth

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
)

// TestMigrateReclaimsLegacyWorkspaceMembers verifies that a workspace_members
// table left behind by the previous owner (shape workspace_id/user_id/role) is
// replaced by the gateway's own shape — instead of being silently kept by
// CREATE TABLE IF NOT EXISTS — and that its bindings are carried over so no
// member, owner included, loses access.
func TestMigrateReclaimsLegacyWorkspaceMembers(t *testing.T) {
	ctx := context.Background()
	dsn := filepath.Join(t.TempDir(), "legacy.db")

	// Simulate the legacy table created by the previous owner.
	seed, err := sql.Open("sqlite", dsn)
	if err != nil {
		t.Fatalf("open seed db: %v", err)
	}
	if _, err := seed.ExecContext(ctx, `CREATE TABLE workspace_members (
    workspace_id VARCHAR(128) NOT NULL,
    user_id      VARCHAR(64)  NOT NULL,
    role         VARCHAR(32)  NOT NULL DEFAULT 'viewer',
    created_at   TIMESTAMP    NULL,
    PRIMARY KEY (workspace_id, user_id)
)`); err != nil {
		t.Fatalf("seed legacy table: %v", err)
	}
	if _, err := seed.ExecContext(ctx,
		`INSERT INTO workspace_members (workspace_id, user_id, role) VALUES
		 ('ws-tenant1-sales', 'u-owner', 'workspace_owner'),
		 ('ws-tenant1-sales', 'u-dev', 'engineer'),
		 ('malformed-id', 'u-ghost', 'viewer')`,
	); err != nil {
		t.Fatalf("seed legacy rows: %v", err)
	}
	if err := seed.Close(); err != nil {
		t.Fatalf("close seed db: %v", err)
	}

	store, err := NewStore(DialectSQLite, dsn)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	defer store.Close()

	for _, column := range []string{"role_code", "tenant_id", "principal_type"} {
		present, err := store.DB().ColumnExists(ctx, "workspace_members", column)
		if err != nil {
			t.Fatalf("columnExists(%s): %v", column, err)
		}
		if !present {
			t.Errorf("column %q missing: the legacy table was not reclaimed", column)
		}
	}

	rows, err := store.DB().QueryContext(ctx,
		`SELECT tenant_id, workspace_id, user_id, role_code
		   FROM workspace_members ORDER BY user_id`)
	if err != nil {
		t.Fatalf("query bindings: %v", err)
	}
	defer rows.Close()

	type binding struct{ tenantID, workspaceID, userID, roleCode string }
	var got []binding
	for rows.Next() {
		var b binding
		if err := rows.Scan(&b.tenantID, &b.workspaceID, &b.userID, &b.roleCode); err != nil {
			t.Fatalf("scan binding: %v", err)
		}
		got = append(got, b)
	}

	want := []binding{
		{"tenant1", "ws-tenant1-sales", "u-dev", "engineer"},
		{"tenant1", "ws-tenant1-sales", "u-owner", "workspace_owner"},
	}
	if len(got) != len(want) {
		t.Fatalf("carried bindings = %d (%v), want %d", len(got), got, len(want))
	}
	for i, w := range want {
		if got[i] != w {
			t.Errorf("binding[%d] = %+v, want %+v", i, got[i], w)
		}
	}
}

// TestMigrateKeepsCurrentWorkspaceMembers makes sure the reclamation only
// targets a stale shape: running the migration twice must not drop live data.
func TestMigrateKeepsCurrentWorkspaceMembers(t *testing.T) {
	ctx := context.Background()
	dsn := filepath.Join(t.TempDir(), "stable.db")

	store, err := NewStore(DialectSQLite, dsn)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	if _, err := store.DB().ExecContext(ctx,
		`INSERT INTO workspace_members
		     (id, tenant_id, workspace_id, principal_type, user_id, role_code,
		      granted_by, created_at, updated_at)
		 VALUES ('b1', 't1', 'ws-1', 'user', 'u1', 'engineer', 'u0', 'now', 'now')`,
	); err != nil {
		t.Fatalf("insert binding: %v", err)
	}
	store.Close()

	reopened, err := NewStore(DialectSQLite, dsn)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer reopened.Close()

	var count int
	if err := reopened.DB().QueryRowContext(ctx,
		`SELECT COUNT(*) FROM workspace_members`).Scan(&count); err != nil {
		t.Fatalf("count bindings: %v", err)
	}
	if count != 1 {
		t.Errorf("binding count = %d, want 1 (existing data must survive)", count)
	}
}
