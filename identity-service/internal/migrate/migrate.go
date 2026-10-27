// Package migrate applies versioned, ordered SQL migrations to the identity
// database (design 05 §4.2: identity tables move from ad-hoc
// `CREATE TABLE IF NOT EXISTS` to MANAGED migrations).
//
// Why a small in-tree migrator instead of pulling golang-migrate:
//
//   - The platform must stay dialect-agnostic across sqlite / mysql /
//     postgresql, which the DDL already achieves by using only portable
//     column types. golang-migrate would add a dependency and a second
//     dialect abstraction next to the existing `dbx` one.
//   - Migrations are applied by the deployment pipeline, not by the service
//     on every boot, so the feature surface needed is exactly: "which
//     versions were applied, apply the missing ones in order, atomically
//     record each".
//
// Contract:
//   - files are embedded and named `NNNN_description.sql` (4-digit version);
//   - a file is applied at most once, recorded in `schema_migrations`;
//   - statements inside one file are split on `;` at line ends and executed
//     sequentially — MySQL rejects multi-statement Exec;
//   - a file that fails aborts the run: later versions are NOT applied, so
//     the database never ends up half-way through an ordered chain.
package migrate

import (
	"context"
	"embed"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/dbx"
)

//go:embed sql/*.sql
var migrationFS embed.FS

// Migration is one versioned SQL file.
type Migration struct {
	Version    int
	Name       string
	Statements []Statement
}

// Statement is one SQL statement plus its optional application guard.
//
// Guards exist because the three supported dialects do not agree on
// idempotent DDL: `CREATE TABLE IF NOT EXISTS` is universal, but
// `ADD COLUMN IF NOT EXISTS` is PostgreSQL-only, and MySQL/SQLite have no
// equivalent. Rather than fork the SQL per dialect, a statement can declare
// what must be ABSENT for it to run, and the migrator probes the catalog
// through dbx introspection (which already abstracts the three dialects).
//
// Directive syntax, on the line before the statement:
//
//	-- +migrate ifNoColumn users.timezone
//	-- +migrate ifNoTable cells
type Statement struct {
	SQL string
	// GuardKind is "", "ifNoColumn" or "ifNoTable".
	GuardKind string
	// GuardTable / GuardColumn are the probe target.
	GuardTable  string
	GuardColumn string
}

const (
	guardIfNoColumn = "ifNoColumn"
	guardIfNoTable  = "ifNoTable"
	directivePrefix = "-- +migrate "
)

// Run applies every migration not yet recorded, in version order.
//
// Returns the versions applied by this call (empty when already current), so
// the caller can log precisely what a deployment changed.
func Run(ctx context.Context, db *dbx.DB, logger *zap.Logger) ([]int, error) {
	if err := ensureLedger(ctx, db); err != nil {
		return nil, fmt.Errorf("ensure schema_migrations: %w", err)
	}
	applied, err := appliedVersions(ctx, db)
	if err != nil {
		return nil, fmt.Errorf("read applied versions: %w", err)
	}
	all, err := load()
	if err != nil {
		return nil, err
	}

	var ran []int
	for _, m := range all {
		if _, done := applied[m.Version]; done {
			continue
		}
		executed := 0
		for _, stmt := range m.Statements {
			run, guardErr := shouldRun(ctx, db, stmt)
			if guardErr != nil {
				return ran, fmt.Errorf("migration %04d (%s) guard failed: %w",
					m.Version, m.Name, guardErr)
			}
			if !run {
				continue
			}
			if _, execErr := db.ExecContext(ctx, stmt.SQL); execErr != nil {
				// Abort the chain: applying a later version onto a failed
				// earlier one produces a schema no migration describes.
				return ran, fmt.Errorf("migration %04d (%s) statement failed: %w\nstatement: %s",
					m.Version, m.Name, execErr, truncate(stmt.SQL, 200))
			}
			executed++
		}
		if err := record(ctx, db, m); err != nil {
			return ran, fmt.Errorf("record migration %04d: %w", m.Version, err)
		}
		ran = append(ran, m.Version)
		logger.Info("migration_applied",
			zap.Int("version", m.Version),
			zap.String("name", m.Name),
			zap.Int("statements_executed", executed),
			zap.Int("statements_total", len(m.Statements)))
	}
	return ran, nil
}

// shouldRun evaluates a statement's guard against the live schema.
func shouldRun(ctx context.Context, db *dbx.DB, stmt Statement) (bool, error) {
	switch stmt.GuardKind {
	case "":
		return true, nil
	case guardIfNoColumn:
		exists, err := db.ColumnExists(ctx, stmt.GuardTable, stmt.GuardColumn)
		return !exists, err
	case guardIfNoTable:
		exists, err := db.TableExists(ctx, stmt.GuardTable)
		return !exists, err
	default:
		return false, fmt.Errorf("unknown migrate guard %q", stmt.GuardKind)
	}
}

// Pending reports versions present in the binary but not yet in the database.
// The service calls this on boot and REFUSES to serve when it is non-empty:
// running application code against an older schema is how silent data
// corruption starts.
func Pending(ctx context.Context, db *dbx.DB) ([]int, error) {
	if err := ensureLedger(ctx, db); err != nil {
		return nil, err
	}
	applied, err := appliedVersions(ctx, db)
	if err != nil {
		return nil, err
	}
	all, err := load()
	if err != nil {
		return nil, err
	}
	var pending []int
	for _, m := range all {
		if _, done := applied[m.Version]; !done {
			pending = append(pending, m.Version)
		}
	}
	return pending, nil
}

func ensureLedger(ctx context.Context, db *dbx.DB) error {
	// Dialect-neutral on purpose (see package doc): VARCHAR keys, UTC RFC3339
	// timestamps as text.
	_, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR(16)  NOT NULL PRIMARY KEY,
    name       VARCHAR(128) NOT NULL,
    applied_at VARCHAR(64)  NOT NULL
)`)
	return err
}

func appliedVersions(ctx context.Context, db *dbx.DB) (map[int]struct{}, error) {
	rows, err := db.QueryContext(ctx, `SELECT version FROM schema_migrations`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	applied := map[int]struct{}{}
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		v, convErr := strconv.Atoi(strings.TrimSpace(raw))
		if convErr != nil {
			continue
		}
		applied[v] = struct{}{}
	}
	return applied, rows.Err()
}

func record(ctx context.Context, db *dbx.DB, m Migration) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)`,
		fmt.Sprintf("%04d", m.Version), m.Name, time.Now().UTC().Format(time.RFC3339))
	return err
}

// load parses the embedded SQL files into ordered migrations.
func load() ([]Migration, error) {
	entries, err := migrationFS.ReadDir("sql")
	if err != nil {
		return nil, fmt.Errorf("read embedded migrations: %w", err)
	}

	var out []Migration
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".sql") {
			continue
		}
		versionPart, rest, found := strings.Cut(strings.TrimSuffix(name, ".sql"), "_")
		if !found {
			return nil, fmt.Errorf("migration %q must be named NNNN_description.sql", name)
		}
		version, convErr := strconv.Atoi(versionPart)
		if convErr != nil {
			return nil, fmt.Errorf("migration %q has a non-numeric version: %w", name, convErr)
		}
		raw, readErr := migrationFS.ReadFile("sql/" + name)
		if readErr != nil {
			return nil, readErr
		}
		out = append(out, Migration{
			Version:    version,
			Name:       rest,
			Statements: splitStatements(string(raw)),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Version < out[j].Version })
	return out, nil
}

// splitStatements splits a migration file into individual statements and
// attaches any `-- +migrate <guard>` directive that precedes one.
//
// Splitting on ";" at END OF LINE only: that keeps semicolons inside string
// literals or comments from cutting a statement in half, which a naive
// strings.Split(";") would do.
func splitStatements(content string) []Statement {
	var (
		statements []Statement
		current    strings.Builder
		pending    Statement
	)
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(line)

		// Directive lines configure the NEXT statement.
		if strings.HasPrefix(trimmed, directivePrefix) {
			kind, table, column := parseDirective(strings.TrimPrefix(trimmed, directivePrefix))
			pending.GuardKind, pending.GuardTable, pending.GuardColumn = kind, table, column
			continue
		}
		if trimmed == "" || strings.HasPrefix(trimmed, "--") {
			continue
		}

		current.WriteString(line)
		current.WriteString("\n")
		if strings.HasSuffix(trimmed, ";") {
			sql := strings.TrimSuffix(strings.TrimSpace(current.String()), ";")
			if sql != "" {
				pending.SQL = sql
				statements = append(statements, pending)
			}
			current.Reset()
			pending = Statement{}
		}
	}
	if leftover := strings.TrimSpace(current.String()); leftover != "" {
		pending.SQL = leftover
		statements = append(statements, pending)
	}
	return statements
}

// parseDirective reads "ifNoColumn users.timezone" / "ifNoTable cells".
func parseDirective(body string) (kind, table, column string) {
	fields := strings.Fields(body)
	if len(fields) < 2 {
		return "", "", ""
	}
	kind = fields[0]
	target := fields[1]
	if tbl, col, found := strings.Cut(target, "."); found {
		return kind, tbl, col
	}
	return kind, target, ""
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
