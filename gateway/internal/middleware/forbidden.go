package middleware

import (
	"encoding/json"
	"fmt"
	"net/http"
)

// DenyReason explains WHY authorization failed. It is part of the HTTP contract:
// a bare 403 forces the client to guess, while a reason lets it render an
// actionable message ("your role lacks X", "you are not a member of this
// workspace", ...) and lets support diagnose a ticket from a screenshot.
type DenyReason string

const (
	// DenyNoTenant: the identity carries no active organization, so no role can
	// be resolved. Happens when a tenant was not selected after login.
	DenyNoTenant DenyReason = "no_active_tenant"
	// DenyRouteNotAllowed: the endpoint is not mapped to a permission and the
	// default-deny fallback applied.
	DenyRouteNotAllowed DenyReason = "route_not_allowed"
	// DenyMissingPermission: the caller's role does not grant the permission.
	DenyMissingPermission DenyReason = "missing_permission"
	// DenyWorkspaceNotMember: the caller holds no role in the target workspace.
	DenyWorkspaceNotMember DenyReason = "workspace_not_member"
	// DenyWorkspaceUnresolved: the request does not identify which workspace it
	// targets, so a workspace-scoped permission cannot be granted.
	DenyWorkspaceUnresolved DenyReason = "workspace_unresolved"
	// DenyWorkspacePathForeign: the working-copy path belongs to another user.
	DenyWorkspacePathForeign DenyReason = "workspace_path_foreign"
)

// forbiddenBody is the single shape of every authorization rejection produced by
// the gateway. `message` is display-ready English; `reason` and
// `requiredPermission` let the frontend localize and name the missing
// capability.
type forbiddenBody struct {
	Code               string     `json:"code"`
	Reason             DenyReason `json:"reason"`
	Message            string     `json:"message"`
	RequiredPermission string     `json:"requiredPermission,omitempty"`
}

// WriteForbidden replies with 403 and a self-explanatory payload.
//
// permission may be empty for reasons that are not tied to a permission code
// (e.g. no active tenant).
func WriteForbidden(w http.ResponseWriter, reason DenyReason, permission string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(w).Encode(forbiddenBody{
		Code:               "forbidden",
		Reason:             reason,
		Message:            denyMessage(reason, permission),
		RequiredPermission: permission,
	})
}

// denyMessage renders the English fallback message shown when the client does
// not localize the reason.
func denyMessage(reason DenyReason, permission string) string {
	switch reason {
	case DenyNoTenant:
		return "No active organization in your session. Select an organization and try again."
	case DenyRouteNotAllowed:
		return "This operation is not available: it is not mapped to any permission."
	case DenyWorkspaceNotMember:
		return "Access denied: you are not a member of this workspace." +
			requiredSuffix(permission)
	case DenyWorkspaceUnresolved:
		return "Access denied: the workspace this request targets could not be determined." +
			requiredSuffix(permission)
	case DenyWorkspacePathForeign:
		return "Access denied: this working copy belongs to another user."
	default:
		if permission == "" {
			return "Permission denied: your role does not allow this operation."
		}
		return fmt.Sprintf(
			"Permission denied: your role does not grant %q, which this operation requires.",
			permission)
	}
}

// requiredSuffix appends the permission a denial was evaluated against, so the
// message names the capability even when the reason is about membership.
func requiredSuffix(permission string) string {
	if permission == "" {
		return ""
	}
	return fmt.Sprintf(" Required permission: %q.", permission)
}
