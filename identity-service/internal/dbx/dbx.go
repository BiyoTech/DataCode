// Package dbx is the gateway's SQL dialect abstraction.
//
// The gateway persists identities, tenants and RBAC bindings, and must run on
// any of the platform's three supported backends: SQLite (zero-dependency
// dev), MySQL and PostgreSQL. Those differ in three places that would
// otherwise leak into every query in the auth and rbac domains:
//
//  1. **Placeholders.** SQLite and MySQL bind with `?`; PostgreSQL binds with
//     `$1, $2, ...`. Rewriting call sites per dialect would mean maintaining
//     three copies of every statement, so instead all SQL is written once in
//     `?` form and rebound on the way to the driver (see Rebind).
//  2. **Introspection.** "Does this table/column/index exist" has a different
//     answer path in each backend.
//  3. **Error identity.** A unique-constraint violation — which the seeders
//     rely on to make concurrent replica startup idempotent — surfaces with a
//     different code and message in each driver.
//
// The DB and Tx wrappers expose the same method set as database/sql, so
// domain code reads identically regardless of backend and cannot forget to
// rebind: there is no path to the raw handle in normal use.
package dbx

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

// Dialect identifies the SQL backend in use.
type Dialect string

const (
	SQLite   Dialect = "sqlite"
	MySQL    Dialect = "mysql"
	Postgres Dialect = "postgres"
)

// Normalize maps a configured database type onto a Dialect.
//
// It accepts the spellings used across the platform's config files and the
// Python services ("postgresql") alongside the driver-level names, so
// platform.yaml can stay a single source of truth for every language.
// An unknown value yields ok=false rather than a silent default: guessing the
// backend would surface as unintelligible SQL syntax errors much later.
func Normalize(dbType string) (Dialect, bool) {
	switch strings.ToLower(strings.TrimSpace(dbType)) {
	case "sqlite", "sqlite3":
		return SQLite, true
	case "mysql", "mariadb":
		return MySQL, true
	case "postgres", "postgresql", "pgx":
		return Postgres, true
	default:
		return "", false
	}
}

// DriverName returns the database/sql driver registered for the dialect.
func DriverName(d Dialect) (string, error) {
	switch d {
	case SQLite:
		return "sqlite", nil
	case MySQL:
		return "mysql", nil
	case Postgres:
		// pgx's database/sql compatibility layer, registered as "pgx" by
		// importing github.com/jackc/pgx/v5/stdlib.
		return "pgx", nil
	default:
		return "", fmt.Errorf("unsupported database dialect: %s", d)
	}
}

// Rebind converts a `?`-placeholder query into the dialect's own form.
//
// Only PostgreSQL needs the rewrite; the others get the query untouched.
// Quoted string literals are skipped so a legitimate `?` inside a literal is
// never mistaken for a bind marker — without that guard a query would silently
// shift all following parameter numbers by one.
func Rebind(d Dialect, query string) string {
	if d != Postgres || !strings.ContainsRune(query, '?') {
		return query
	}

	var (
		out     strings.Builder
		n       int
		inQuote rune
	)
	out.Grow(len(query) + 8)

	for _, r := range query {
		switch {
		case inQuote != 0:
			// Inside a literal: copy verbatim until the matching quote. SQL
			// escapes a quote by doubling it, which this handles naturally —
			// the pair closes then immediately reopens the literal.
			if r == inQuote {
				inQuote = 0
			}
			out.WriteRune(r)
		case r == '\'' || r == '"':
			inQuote = r
			out.WriteRune(r)
		case r == '?':
			n++
			out.WriteString("$")
			out.WriteString(fmt.Sprint(n))
		default:
			out.WriteRune(r)
		}
	}
	return out.String()
}

// Placeholders renders the bind markers for an IN clause of n values, in the
// `?` form that Rebind later converts. Returns "" for n <= 0 so callers can
// detect and skip an empty IN list, which is not valid SQL.
func Placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

// ---------------------------------------------------------------------------
// Handles
// ---------------------------------------------------------------------------

// DB is a *sql.DB that rebinds every statement for its dialect.
type DB struct {
	db      *sql.DB
	dialect Dialect
}

// Wrap adopts an existing handle. Used by domains that share the gateway's
// single connection pool (rbac reuses the auth handle) instead of opening a
// second one against the same database.
func Wrap(db *sql.DB, dialect Dialect) *DB {
	return &DB{db: db, dialect: dialect}
}

// Dialect returns the backend this handle talks to.
func (d *DB) Dialect() Dialect { return d.dialect }

// SQL exposes the raw handle. Reserved for the few operations that must not be
// rebound (driver-level probes); domain code should not need it.
func (d *DB) SQL() *sql.DB { return d.db }

// Close releases the underlying handle.
func (d *DB) Close() error { return d.db.Close() }

// ExecContext runs a statement with dialect-appropriate placeholders.
func (d *DB) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	return d.db.ExecContext(ctx, Rebind(d.dialect, query), args...)
}

// QueryContext runs a query with dialect-appropriate placeholders.
func (d *DB) QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error) {
	return d.db.QueryContext(ctx, Rebind(d.dialect, query), args...)
}

// QueryRowContext runs a single-row query with dialect-appropriate placeholders.
func (d *DB) QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row {
	return d.db.QueryRowContext(ctx, Rebind(d.dialect, query), args...)
}

// BeginTx starts a transaction whose statements are rebound the same way.
func (d *DB) BeginTx(ctx context.Context, opts *sql.TxOptions) (*Tx, error) {
	tx, err := d.db.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &Tx{tx: tx, dialect: d.dialect}, nil
}

// Tx is a *sql.Tx that rebinds every statement for its dialect.
type Tx struct {
	tx      *sql.Tx
	dialect Dialect
}

// ExecContext runs a statement inside the transaction.
func (t *Tx) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	return t.tx.ExecContext(ctx, Rebind(t.dialect, query), args...)
}

// QueryContext runs a query inside the transaction.
func (t *Tx) QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error) {
	return t.tx.QueryContext(ctx, Rebind(t.dialect, query), args...)
}

// QueryRowContext runs a single-row query inside the transaction.
func (t *Tx) QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row {
	return t.tx.QueryRowContext(ctx, Rebind(t.dialect, query), args...)
}

// Commit applies the transaction.
func (t *Tx) Commit() error { return t.tx.Commit() }

// Rollback discards the transaction.
func (t *Tx) Rollback() error { return t.tx.Rollback() }
