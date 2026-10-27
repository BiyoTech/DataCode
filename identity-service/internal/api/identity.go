// Package api — the identity context this service publishes on its own requests.
//
// Scope is deliberately narrow: this service authenticates a caller and answers
// questions about identity and roles. It performs no workspace-scoped
// authorization — that is the gateway's job, decided against the snapshot this
// service serves — so there are no workspace or permission keys here. The copies
// that used to exist were unused leftovers from the gateway's middleware and
// invited a second, divergent enforcement point.
package api

import (
	"context"
)

// contextKey is a private type for context keys to avoid collisions.
type contextKey string

const (
	// UserIDKey is the context key for the authenticated user ID.
	UserIDKey contextKey = "user_id"
	// TenantIDKey is the context key for the tenant ID of the active identity.
	TenantIDKey contextKey = "tenant_id"
	// UsernameKey is the context key for the authenticated username.
	UsernameKey contextKey = "username"
	// RoleKey is the context key for the user's role within the active tenant.
	RoleKey contextKey = "role"
)

// GetUserID extracts the user ID from request context.
func GetUserID(ctx context.Context) string {
	return stringFromContext(ctx, UserIDKey)
}

// GetTenantID extracts the tenant ID from request context.
func GetTenantID(ctx context.Context) string {
	return stringFromContext(ctx, TenantIDKey)
}

// GetUsername extracts the username from request context.
func GetUsername(ctx context.Context) string {
	return stringFromContext(ctx, UsernameKey)
}

// GetRole extracts the tenant-scoped role from request context.
func GetRole(ctx context.Context) string {
	return stringFromContext(ctx, RoleKey)
}

func stringFromContext(ctx context.Context, key contextKey) string {
	if v, ok := ctx.Value(key).(string); ok {
		return v
	}
	return ""
}
