package rbac

import (
	"path"
	"strings"
)

// workspaceIDPrefix mirrors the identifier convention owned by the workspace
// service: a workspace id is always "ws-{tenant_id}-{slug}".
const workspaceIDPrefix = "ws-"

// envsDirName mirrors ENVS_DIR in workspace-runtime's path layout
// (datacode_common.workspace_paths): every environment working copy is a Git
// worktree living under this directory.
const envsDirName = "envs"

// environmentIDSeparator mirrors naming.environment_id() in the shared
// datacode_common naming module: an environment id is always
// "{workspace_id}:{environment}".
const environmentIDSeparator = ":"

// workingCopyDepth is the number of segments a working copy occupies below the
// base directory: {tenant_id}/{workspace_id}/{user_id}/envs/{environment}.
const workingCopyDepth = 5

// WorkingCopy is the platform identity encoded in a `workspace_path`.
//
// The path is the only parameter downstream services receive, so it must carry
// the full addressing tuple: which tenant, which workspace, whose checkout and
// which environment. Deriving these here — instead of trusting request fields —
// is what lets the gateway authorize a request before any service is reached.
type WorkingCopy struct {
	TenantID    string
	WorkspaceID string
	UserID      string
	Environment string
}

// BelongsToTenant reports whether a workspace id was minted for a tenant.
// Because the tenant is encoded in the identifier, cross-tenant access can be
// rejected without touching the database.
func BelongsToTenant(workspaceID, tenantID string) bool {
	if workspaceID == "" || tenantID == "" {
		return false
	}
	return strings.HasPrefix(workspaceID, workspaceIDPrefix+tenantID+"-")
}

// ParseWorkingCopy decomposes a working-copy path into its addressing tuple.
//
// The layout is "{base}/{tenant_id}/{workspace_id}/{user_id}/envs/{environment}"
// (see the workspace service path conventions), so the segments are read from
// the END of the path: the base directory is a deployment setting the gateway
// does not know. The "envs" level is asserted, which rejects the shared object
// database (".repo"), the user root itself and any arbitrary nested folder.
//
// Only structural validation happens here; the environment naming rules stay
// owned by the sqlmesh service, which re-validates every path it receives.
func ParseWorkingCopy(workspacePath string) (WorkingCopy, bool) {
	if workspacePath == "" {
		return WorkingCopy{}, false
	}
	// Clean resolves "..", so a traversal cannot smuggle in another tenant's
	// directory: the caller-visible segments are the effective ones.
	cleaned := path.Clean(strings.ReplaceAll(workspacePath, "\\", "/"))
	segments := strings.Split(strings.Trim(cleaned, "/"), "/")
	if len(segments) < workingCopyDepth {
		return WorkingCopy{}, false
	}

	tail := segments[len(segments)-workingCopyDepth:]
	if tail[3] != envsDirName {
		return WorkingCopy{}, false
	}
	copyInfo := WorkingCopy{
		TenantID:    tail[0],
		WorkspaceID: tail[1],
		UserID:      tail[2],
		Environment: tail[4],
	}
	if copyInfo.TenantID == "" || copyInfo.UserID == "" || copyInfo.Environment == "" {
		return WorkingCopy{}, false
	}
	return copyInfo, true
}

// WorkspaceIDFromPath derives the workspace id a working copy belongs to.
//
// The tenant encoded in both the directory layout and the workspace identifier
// must match the caller's tenant, which stops one tenant from addressing
// another tenant's checkout.
func WorkspaceIDFromPath(workspacePath, tenantID string) (string, bool) {
	if tenantID == "" {
		return "", false
	}
	working, ok := ParseWorkingCopy(workspacePath)
	if !ok {
		return "", false
	}
	if working.TenantID != tenantID || !BelongsToTenant(working.WorkspaceID, tenantID) {
		return "", false
	}
	return working.WorkspaceID, true
}

// OwnsWorkingCopy reports whether a working copy belongs to a user.
//
// A working copy is private: it holds that user's uncommitted work, so one
// member must never operate on another member's checkout even when both are
// members of the same workspace.
func OwnsWorkingCopy(workspacePath, userID string) bool {
	if userID == "" {
		return false
	}
	working, ok := ParseWorkingCopy(workspacePath)
	if !ok {
		return false
	}
	return working.UserID == userID
}

// WorkspaceIDFromEnvironmentID derives the workspace an environment belongs to.
//
// Environment ids are "{workspace_id}:{environment}", so the workspace — and
// with it the role to authorize against — is available without a database
// lookup. The environment part is not validated here: its naming rules are
// owned by the sqlmesh service, which rejects unknown environments with a 404.
func WorkspaceIDFromEnvironmentID(environmentID, tenantID string) (string, bool) {
	if environmentID == "" || tenantID == "" {
		return "", false
	}
	workspaceID, environment, found := strings.Cut(environmentID, environmentIDSeparator)
	if !found || environment == "" || !BelongsToTenant(workspaceID, tenantID) {
		return "", false
	}
	return workspaceID, true
}
