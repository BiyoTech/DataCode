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
	// PermsVersionKey is the context key for the tenant's authorization
	// generation carried in the caller's token. The authz resolver compares it
	// against its cached snapshot so a permission change takes effect on the
	// next request instead of after the cache TTL (design 03 §2.5).
	PermsVersionKey contextKey = "perms_version"
	// WorkspaceIDKey is the context key for the workspace a request targets,
	// resolved by the Authorize middleware. Empty for tenant-scoped requests.
	WorkspaceIDKey contextKey = "workspace_id"
	// PermissionsKey is the context key for the effective permission codes of
	// the scope the request was authorized in.
	//
	// Permissions, not the role that granted them: a role is a tenant-editable
	// composition, so anything branching on a role name would disagree with the
	// role editor the moment a tenant edits it.
	PermissionsKey contextKey = "permissions"
	// WorkspacePathKey is the context key for the working copy a request
	// targets, as read by Authorize.
	//
	// Published so lease-aware routing (design 06 P4a) can reuse it: the value
	// may live in the JSON body, and re-reading the body downstream would mean
	// buffering it twice — and getting the buffering subtly wrong is how a proxy
	// starts forwarding empty POSTs.
	WorkspacePathKey contextKey = "workspace_path"
	// CellIDKey is the context key for the cell the caller's tenant lives in,
	// as carried by the verified token (design 03 §2.4, P7).
	//
	// Empty when the token predates placement, which the cell router treats as
	// "serve locally": rejecting those would log out every existing session on
	// upgrade, and a single-cell install is exactly the case where every tenant
	// is local anyway.
	CellIDKey contextKey = "cell_id"
	// RequestIdentityKey is the context key for the mutable identity slot the
	// access logger installs before authentication runs. See RequestIdentity.
	RequestIdentityKey contextKey = "request_identity"
)

// RequestIdentity is the access log's window into an identity that is only known
// LATER in the chain.
//
// The logger has to wrap the whole request to measure its duration and status,
// so it runs outermost — before authentication. Authentication publishes the
// verified identity on a DERIVED request (r.WithContext), which the logger's
// outer closure can never see: that is why every gateway access log line carried
// an empty user_id.
//
// A pointer installed on the way in and filled on the way through solves it
// without moving the logger after authentication, which would drop the access
// log for 401s and for public routes — the very requests an audit trail needs.
//
// Not synchronized: the value is written once by JWTAuth and read after
// next.ServeHTTP returns, so the handler chain itself orders the two.
type RequestIdentity struct {
	UserID   string
	TenantID string
}

// SetRequestIdentity records the verified identity in the slot the access logger
// installed, if any. Safe to call when no slot exists (direct handler tests).
func SetRequestIdentity(ctx context.Context, userID, tenantID string) {
	if slot, ok := ctx.Value(RequestIdentityKey).(*RequestIdentity); ok && slot != nil {
		slot.UserID = userID
		slot.TenantID = tenantID
	}
}

// GetCellID returns the cell id carried by the caller's token, or "".
func GetCellID(ctx context.Context) string {
	return stringFromContext(ctx, CellIDKey)
}

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

// GetWorkspacePath extracts the working copy a request targets, or "" when the
// route is not working-copy scoped (workspace listing, credentials, auth).
func GetWorkspacePath(ctx context.Context) string {
	return stringFromContext(ctx, WorkspacePathKey)
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

// GetPermsVersion returns the authorization generation from the caller's token,
// or 0 when the token predates the claim (which degrades to TTL-only cache
// staleness rather than failing).
func GetPermsVersion(ctx context.Context) int64 {
	if v, ok := ctx.Value(PermsVersionKey).(int64); ok {
		return v
	}
	return 0
}
