package rbac

import (
	"path"
	"strings"
)

// workspaceIDPrefix mirrors the identifier convention owned by the workspace
// service: a workspace id is always "ws-{tenant_id}-{slug}".
const workspaceIDPrefix = "ws-"

// BelongsToTenant reports whether a workspace id was minted for a tenant.
// Because the tenant is encoded in the identifier, cross-tenant access can be
// rejected without touching the database.
func BelongsToTenant(workspaceID, tenantID string) bool {
	if workspaceID == "" || tenantID == "" {
		return false
	}
	return strings.HasPrefix(workspaceID, workspaceIDPrefix+tenantID+"-")
}

// WorkspaceIDFromPath derives the workspace id from a working-copy path.
//
// Working copies follow the layout "{base}/{tenant_id}/{workspace_id}/{user_id}"
// (see the workspace service path conventions), so the workspace id is the
// second-to-last segment. The tenant encoded in both the directory layout and
// the identifier must match the caller's tenant, which stops one tenant from
// addressing another tenant's checkout.
func WorkspaceIDFromPath(workspacePath, tenantID string) (string, bool) {
	if workspacePath == "" || tenantID == "" {
		return "", false
	}
	cleaned := path.Clean(strings.ReplaceAll(workspacePath, "\\", "/"))
	segments := strings.Split(strings.Trim(cleaned, "/"), "/")
	if len(segments) < 3 {
		return "", false
	}
	workspaceID := segments[len(segments)-2]
	pathTenantID := segments[len(segments)-3]
	if pathTenantID != tenantID || !BelongsToTenant(workspaceID, tenantID) {
		return "", false
	}
	return workspaceID, true
}
