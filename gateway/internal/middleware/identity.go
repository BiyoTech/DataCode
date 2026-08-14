// Package middleware provides HTTP middleware for the API Gateway.
package middleware

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
	// WorkspaceIDKey is the context key for the workspace a request targets,
	// resolved by the Authorize middleware. Empty for tenant-scoped requests.
	WorkspaceIDKey contextKey = "workspace_id"
	// WorkspaceRoleKey is the context key for the caller's role inside the
	// resolved workspace.
	WorkspaceRoleKey contextKey = "workspace_role"
	// PermissionsKey is the context key for the effective permission codes of
	// the scope the request was authorized in.
	PermissionsKey contextKey = "permissions"
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

// GetWorkspaceID extracts the workspace resolved during authorization.
func GetWorkspaceID(ctx context.Context) string {
	return stringFromContext(ctx, WorkspaceIDKey)
}

// GetWorkspaceRole extracts the caller's role inside the resolved workspace.
func GetWorkspaceRole(ctx context.Context) string {
	return stringFromContext(ctx, WorkspaceRoleKey)
}

// GetPermissions extracts the effective permission codes of the authorized
// scope. The slice is never nil so callers can iterate without a guard.
func GetPermissions(ctx context.Context) []string {
	if v, ok := ctx.Value(PermissionsKey).([]string); ok {
		return v
	}
	return []string{}
}

// HasPermission reports whether the authorized scope grants a permission code.
func HasPermission(ctx context.Context, permission string) bool {
	for _, granted := range GetPermissions(ctx) {
		if granted == permission {
			return true
		}
	}
	return false
}

func stringFromContext(ctx context.Context, key contextKey) string {
	if v, ok := ctx.Value(key).(string); ok {
		return v
	}
	return ""
}
