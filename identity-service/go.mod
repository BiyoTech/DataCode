module github.com/datacode/identity-service

go 1.25.0

// Local shared module, not published. The permission catalog MUST be identical
// to the gateway's: this service owns the role data and the gateway takes the
// decisions, so a code that exists on one side only produces a route nobody can
// pass. One package, checked against shared/rbac-catalog.json by tests.
replace github.com/datacode/shared => ../shared/go

require (
	github.com/datacode/shared v0.0.0
	github.com/go-chi/chi/v5 v5.1.0
	github.com/go-sql-driver/mysql v1.10.0
	github.com/golang-jwt/jwt/v5 v5.2.1
	github.com/jackc/pgx/v5 v5.10.0
	go.uber.org/zap v1.27.0
	golang.org/x/crypto v0.31.0
	gopkg.in/yaml.v3 v3.0.1
	modernc.org/sqlite v1.34.5
)

require (
	filippo.io/edwards25519 v1.2.0 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.2 // indirect
	github.com/mattn/go-isatty v0.0.20 // indirect
	github.com/ncruces/go-strftime v0.1.9 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	github.com/rogpeppe/go-internal v1.16.0 // indirect
	go.uber.org/multierr v1.10.0 // indirect
	golang.org/x/sync v0.17.0 // indirect
	golang.org/x/sys v0.30.0 // indirect
	golang.org/x/text v0.29.0 // indirect
	modernc.org/libc v1.55.3 // indirect
	modernc.org/mathutil v1.6.0 // indirect
	modernc.org/memory v1.8.0 // indirect
)
