package migrate

import (
	"context"
	"path/filepath"
	"testing"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/dbx"
)

// openTestDB gives each test its own SQLite file, so migrations run against a
// genuinely empty schema (an in-memory DSN would be shared per connection and
// hide ordering bugs).
func openTestDB(t *testing.T) *dbx.DB {
	t.Helper()
	dsn := filepath.Join(t.TempDir(), "identity.db")
	db, err := dbx.Open(dbx.SQLite, dsn)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestRunAppliesEveryMigrationOnce(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	logger := zap.NewNop()

	applied, err := Run(ctx, db, logger)
	if err != nil {
		t.Fatalf("first run: %v", err)
	}
	if len(applied) == 0 {
		t.Fatal("expected the baseline migrations to be applied")
	}

	// The tables the platform depends on must exist afterwards.
	for _, table := range []string{
		"users", "tenants", "tenant_members", "refresh_tokens",
		"permissions", "roles", "role_permissions", "workspace_members",
		"rbac_audit_log", "cells", "tenant_placement", "tenant_perms_version",
	} {
		exists, existErr := db.TableExists(ctx, table)
		if existErr != nil {
			t.Fatalf("probe %s: %v", table, existErr)
		}
		if !exists {
			t.Errorf("table %q was not created", table)
		}
	}

	// 0002 adds the preference columns.
	for _, column := range []string{"timezone", "locale"} {
		exists, existErr := db.ColumnExists(ctx, "users", column)
		if existErr != nil {
			t.Fatalf("probe users.%s: %v", column, existErr)
		}
		if !exists {
			t.Errorf("users.%s was not added", column)
		}
	}

	// Re-running must be a no-op: this is what makes the migration safe to
	// execute on every deploy.
	second, err := Run(ctx, db, logger)
	if err != nil {
		t.Fatalf("second run: %v", err)
	}
	if len(second) != 0 {
		t.Errorf("second run applied %v, want nothing", second)
	}

	pending, err := Pending(ctx, db)
	if err != nil {
		t.Fatalf("pending: %v", err)
	}
	if len(pending) != 0 {
		t.Errorf("pending after full run = %v, want empty", pending)
	}
}

// The guard directive is what makes ADD COLUMN idempotent across dialects that
// have no ADD COLUMN IF NOT EXISTS. Without it, a second run on a database
// that already has the column would abort the chain.
func TestGuardedStatementIsSkippedWhenColumnExists(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	if _, err := Run(ctx, db, zap.NewNop()); err != nil {
		t.Fatalf("run: %v", err)
	}

	stmt := Statement{
		SQL:         "ALTER TABLE users ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'UTC'",
		GuardKind:   guardIfNoColumn,
		GuardTable:  "users",
		GuardColumn: "timezone",
	}
	run, err := shouldRun(ctx, db, stmt)
	if err != nil {
		t.Fatalf("shouldRun: %v", err)
	}
	if run {
		t.Error("guarded statement should be skipped: users.timezone already exists")
	}
}

func TestPendingReportsUnappliedVersions(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	pending, err := Pending(ctx, db)
	if err != nil {
		t.Fatalf("pending: %v", err)
	}
	if len(pending) == 0 {
		t.Fatal("a fresh database must report pending migrations")
	}
	// Ordering matters: the chain is applied lowest-first.
	for i := 1; i < len(pending); i++ {
		if pending[i-1] >= pending[i] {
			t.Fatalf("pending versions are not ascending: %v", pending)
		}
	}
}

func TestSplitStatementsAttachesGuardToNextStatement(t *testing.T) {
	content := `-- comment line
CREATE TABLE a (id VARCHAR(8));
-- +migrate ifNoColumn users.timezone
ALTER TABLE users ADD COLUMN timezone VARCHAR(64);
CREATE TABLE b (id VARCHAR(8));
`
	statements := splitStatements(content)
	if len(statements) != 3 {
		t.Fatalf("got %d statements, want 3: %#v", len(statements), statements)
	}
	if statements[0].GuardKind != "" {
		t.Errorf("first statement must be unguarded, got %q", statements[0].GuardKind)
	}
	if statements[1].GuardKind != guardIfNoColumn ||
		statements[1].GuardTable != "users" || statements[1].GuardColumn != "timezone" {
		t.Errorf("guard not attached to the ALTER: %#v", statements[1])
	}
	// The guard must apply to exactly one statement, not leak onward.
	if statements[2].GuardKind != "" {
		t.Errorf("guard leaked to the following statement: %#v", statements[2])
	}
}
