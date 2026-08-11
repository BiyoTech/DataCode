// Package middleware provides HTTP middleware for the API Gateway.
package middleware

import (
	"context"
	"net/http"
)

// contextKey is a private type for context keys to avoid collisions.
type contextKey string

const (
	// UserIDKey is the context key for the authenticated user ID.
	UserIDKey contextKey = "user_id"
	// TenantIDKey is the context key for the tenant ID.
	TenantIDKey contextKey = "tenant_id"
)

// Default values used when the corresponding header is absent.
// MVP: No authentication; these defaults allow local development without login.
const (
	defaultUserID   = "user-admin-001"
	defaultTenantID = "tenant-default"
)

// DefaultUser is a middleware that extracts user identity from request headers
// and injects it into the request context for downstream handlers.
//
// Headers:
//   - X-User-ID:   authenticated user ID (falls back to defaultUserID)
//   - X-Tenant-ID: tenant ID (falls back to defaultTenantID)
//
// Future: Replace with JWT validation middleware that decodes the token and
// populates the same context keys.
func DefaultUser(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		userID := r.Header.Get("X-User-ID")
		if userID == "" {
			userID = defaultUserID
		}

		tenantID := r.Header.Get("X-Tenant-ID")
		if tenantID == "" {
			tenantID = defaultTenantID
		}

		ctx := r.Context()
		ctx = context.WithValue(ctx, UserIDKey, userID)
		ctx = context.WithValue(ctx, TenantIDKey, tenantID)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// GetUserID extracts the user ID from request context.
func GetUserID(ctx context.Context) string {
	if v, ok := ctx.Value(UserIDKey).(string); ok {
		return v
	}
	return ""
}

// GetTenantID extracts the tenant ID from request context.
func GetTenantID(ctx context.Context) string {
	if v, ok := ctx.Value(TenantIDKey).(string); ok {
		return v
	}
	return ""
}
