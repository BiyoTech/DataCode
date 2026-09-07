// Package cellx resolves the instance's cell identity (design 03 §2, P1).
//
// A Cell is a complete, self-sufficient platform instance serving a subset
// of tenants. Even while only one cell exists, every gateway replica knows
// its cell id, puts it in every log line and every Redis key, so scaling
// from one cell to five (P7) is a deployment action, not a code change.
//
// This mirrors datacode_common.cell on the Python side; the two must stay
// byte-compatible (same env var, same validation, same default).
package cellx

import (
	"os"
	"regexp"
)

// EnvVar is the deployment-provided cell identity. Same variable name as the
// Python side (DATACODE_CELL_ID).
const EnvVar = "DATACODE_CELL_ID"

// DefaultCellID is the bootstrap default for single-cell installations.
const DefaultCellID = "cell-0"

// cellIDPattern matches a safe single token: lowercase alphanumerics and
// hyphens, 1..32 chars. It must stay compatible with Redis keys, Loki labels
// and DNS names, mirroring the Python-side regex.
var cellIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,31}$`)

// CellID returns this replica's cell id.
//
// A misconfigured value is a programming/deployment error that would fan out
// into Redis keys and log indexes, so it panics loudly at startup instead of
// being silently coerced.
func CellID() string {
	value := os.Getenv(EnvVar)
	if value == "" {
		return DefaultCellID
	}
	if !cellIDPattern.MatchString(value) {
		panic("cellx: " + EnvVar + " must match " + cellIDPattern.String() + ", got " + value)
	}
	return value
}
