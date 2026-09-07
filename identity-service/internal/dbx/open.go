package dbx

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	_ "github.com/go-sql-driver/mysql" // MySQL driver
	_ "github.com/jackc/pgx/v5/stdlib" // PostgreSQL driver (database/sql layer)
	_ "modernc.org/sqlite"             // pure-Go SQLite driver (no CGO)
)

// Pool sizing per dialect.
//
// SQLite serialises writers, so more than one open connection buys nothing and
// turns contention into "database is locked" errors. The networked backends
// are pooled normally.
const (
	sqlitePoolSize = 1
	serverMaxOpen  = 10
	serverMaxIdle  = 5
)

// Open connects to the database and verifies reachability.
//
// dsn is dialect-specific:
//   - SQLite:     a file path (".data/gateway.db"); parent dirs are created.
//   - MySQL:      a go-sql-driver DSN ("user:pass@tcp(host:3306)/db?parseTime=true").
//   - PostgreSQL: a libpq/pgx URL ("postgres://user:pass@host:5432/db?sslmode=disable").
//
// The connection is pinged before returning: a gateway that boots against an
// unreachable database would otherwise pass its startup checks and fail every
// login instead.
func Open(dialect Dialect, dsn string) (*DB, error) {
	if dsn == "" {
		return nil, errors.New("database dsn is empty")
	}

	driver, err := DriverName(dialect)
	if err != nil {
		return nil, err
	}
	if err := prepare(dialect, dsn); err != nil {
		return nil, err
	}

	db, err := sql.Open(driver, dsn)
	if err != nil {
		return nil, fmt.Errorf("open %s database: %w", dialect, err)
	}

	if dialect == SQLite {
		db.SetMaxOpenConns(sqlitePoolSize)
	} else {
		db.SetMaxOpenConns(serverMaxOpen)
		db.SetMaxIdleConns(serverMaxIdle)
	}

	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping %s database: %w", dialect, err)
	}
	return Wrap(db, dialect), nil
}

// prepare performs any pre-open setup a dialect requires.
func prepare(dialect Dialect, dsn string) error {
	if dialect != SQLite {
		return nil
	}
	// SQLite will not create missing parent directories for its file.
	dir := filepath.Dir(dsn)
	if dir == "" || dir == "." {
		return nil
	}
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return fmt.Errorf("create database directory: %w", err)
	}
	return nil
}
