package rbac

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/datacode/identity-service/internal/dbx"
)

// newID returns a random identifier for database primary keys.
func newID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand failure is unrecoverable for authorization records.
		panic(fmt.Sprintf("crypto/rand unavailable: %v", err))
	}
	return hex.EncodeToString(buf)
}

// formatUTC renders a timestamp for storage (always UTC, RFC3339 nanos), which
// matches the convention used by the auth schema.
func formatUTC(t time.Time) string {
	return t.UTC().Format(time.RFC3339Nano)
}

// nowUTC is the single source of "now" for persisted rows.
func nowUTC() string {
	return formatUTC(time.Now())
}

// isUniqueViolation reports whether err is a UNIQUE constraint violation on
// any supported backend. Detection lives in dbx so the three dialects' error
// identities are described in exactly one place.
func isUniqueViolation(err error) bool {
	return dbx.IsUniqueViolation(err)
}

// placeholders renders "?, ?, ?" for an IN clause of n bound values. The `?`
// form is dialect-neutral here: dbx rebinds it on the way to the driver.
func placeholders(n int) string {
	return dbx.Placeholders(n)
}

// dedupe returns values without duplicates, preserving the original order.
func dedupe(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, v := range values {
		if _, ok := seen[v]; ok {
			continue
		}
		seen[v] = struct{}{}
		out = append(out, v)
	}
	return out
}

// ctxWithTimeout is used by background maintenance calls (seeding) so a stalled
// database cannot block process startup indefinitely.
func ctxWithTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, 30*time.Second)
}
