package rbac

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"
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
// either SQLite or MySQL.
func isUniqueViolation(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "UNIQUE constraint failed") ||
		strings.Contains(msg, "Error 1062") ||
		strings.Contains(msg, "Duplicate entry")
}

// placeholders renders "?, ?, ?" for an IN clause of n bound values.
func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
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
