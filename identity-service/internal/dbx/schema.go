package dbx

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/go-sql-driver/mysql"
	"github.com/jackc/pgx/v5/pgconn"
)

// pgUniqueViolation is PostgreSQL's SQLSTATE for a unique constraint breach.
const pgUniqueViolation = "23505"

// mysqlDuplicateEntry is MySQL's error number for a duplicate key.
const mysqlDuplicateEntry = 1062

// IsUniqueViolation reports whether err is a unique-constraint violation.
//
// This is load-bearing rather than cosmetic: the permission catalog and the
// built-in role seeders run on every replica at startup and rely on "the row
// already exists" being distinguishable from a real failure. Misclassifying it
// would either crash a replica on a benign race or swallow a genuine error.
//
// MySQL and PostgreSQL are matched on their typed driver errors. SQLite's
// pure-Go driver does not expose a stable error type for this, so it is
// matched on the message it is documented to produce.
func IsUniqueViolation(err error) bool {
	if err == nil {
		return false
	}

	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == pgUniqueViolation
	}

	var myErr *mysql.MySQLError
	if errors.As(err, &myErr) {
		return myErr.Number == mysqlDuplicateEntry
	}

	return strings.Contains(err.Error(), "UNIQUE constraint failed")
}

// ---------------------------------------------------------------------------
// Schema introspection
//
// The gateway creates its own schema at startup (there is no migration tool on
// the Go side), so it needs idempotency checks that work on all three
// backends.
// ---------------------------------------------------------------------------

// TableExists reports whether a table is present in the current database.
func (d *DB) TableExists(ctx context.Context, table string) (bool, error) {
	var query string
	switch d.dialect {
	case SQLite:
		query = `SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?`
	case Postgres:
		// current_schema() rather than a hardcoded "public": the deployment may
		// route the gateway to a dedicated schema via the DSN search_path.
		query = `SELECT COUNT(*) FROM information_schema.tables
		          WHERE table_schema = current_schema() AND table_name = ?`
	default:
		query = `SELECT COUNT(*) FROM information_schema.tables
		          WHERE table_schema = DATABASE() AND table_name = ?`
	}

	var count int
	if err := d.QueryRowContext(ctx, query, table).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

// ColumnExists reports whether a table carries a given column.
func (d *DB) ColumnExists(ctx context.Context, table, column string) (bool, error) {
	var query string
	switch d.dialect {
	case SQLite:
		query = `SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?`
	case Postgres:
		query = `SELECT COUNT(*) FROM information_schema.columns
		          WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?`
	default:
		query = `SELECT COUNT(*) FROM information_schema.columns
		          WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?`
	}

	var count int
	if err := d.QueryRowContext(ctx, query, table, column).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

// CreateIndexIfNotExists creates an index idempotently.
//
// indexDef is written as "<index_name> ON <table> (<columns>)" — the index name
// has to be separable because MySQL cannot express the idempotency in DDL and
// must be asked whether the index is already there.
func (d *DB) CreateIndexIfNotExists(ctx context.Context, indexDef string) error {
	name, target, err := splitIndexDef(indexDef)
	if err != nil {
		return err
	}

	// SQLite and PostgreSQL both support the guard natively.
	if d.dialect == SQLite || d.dialect == Postgres {
		_, err := d.ExecContext(ctx,
			fmt.Sprintf("CREATE INDEX IF NOT EXISTS %s ON %s", name, target))
		return err
	}

	// MySQL: no IF NOT EXISTS for indexes, so probe the catalog first.
	table, _, found := strings.Cut(target, " ")
	if !found && table == "" {
		return fmt.Errorf("malformed index definition: %q", indexDef)
	}
	var count int
	if err := d.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM information_schema.statistics
		  WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?`,
		table, name).Scan(&count); err != nil {
		return err
	}
	if count > 0 {
		return nil
	}
	_, err = d.ExecContext(ctx, fmt.Sprintf("CREATE INDEX %s ON %s", name, target))
	return err
}

// splitIndexDef parses "<name> ON <table> (<columns>)" into its two halves.
func splitIndexDef(indexDef string) (name, target string, err error) {
	before, after, found := strings.Cut(indexDef, " ON ")
	if !found {
		return "", "", fmt.Errorf("malformed index definition: %q", indexDef)
	}
	name = strings.TrimSpace(before)
	target = strings.TrimSpace(after)
	if name == "" || target == "" {
		return "", "", fmt.Errorf("malformed index definition: %q", indexDef)
	}
	return name, target, nil
}
