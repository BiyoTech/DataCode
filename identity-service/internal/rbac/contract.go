// Package rbac implements the platform's database-driven RBAC model and owns its
// tables: the permission projection, the roles and their composition, the role
// bindings and the audit log.
//
// Roles are stored in two flavours:
//   - system templates (tenant_id == "") — written only by the seeder;
//   - tenant instances (tenant_id == "<tid>") — cloned from the templates the
//     first time a tenant is resolved and never touched by the seeder again.
//
// That separation is what allows a tenant to edit the permission set of a
// built-in role without the change being reverted on the next startup.
package rbac

import (
	catalog "github.com/datacode/shared/rbac"
)

// This file is the identity service's local vocabulary for the permission
// CATALOG, which lives in the shared module because the gateway must agree on it
// symbol for symbol (two hand-written copies is how a route ends up requiring a
// permission that no role can hold).
//
// Type ALIASES, not wrappers: Scope is woven through this package's Role struct
// and ~60 SQL-layer signatures, and a distinct local type would force a
// conversion at every one of them for no benefit. The aliases keep store.go and
// service.go reading as the data owner's code while there is still exactly one
// declaration of what a permission is.
//
// Responsibility split, enforced here:
//
//   - The CATALOG is code (generated from shared/rbac-catalog.json) and is
//     synchronized into the permissions table on startup by Store.SyncPermissions.
//     The table is a PROJECTION: it renders the role editor and keeps historical
//     role compositions readable after a code is retired. It is never written
//     through the API, so a permission code cannot be invented by writing to the
//     database.
//   - ROLE COMPOSITION (roles, role_permissions) is owned by the database and is
//     tenant-editable, including for the built-in roles. That is why the seeder
//     writes templates (tenant_id = "") and clones them per tenant exactly once.

// Authorization scope and catalog record types.
type (
	Scope         = catalog.Scope
	PermissionDef = catalog.PermissionDef
	RoleTemplate  = catalog.RoleTemplate
)

// Scope values.
const (
	ScopeTenant    = catalog.ScopeTenant
	ScopeWorkspace = catalog.ScopeWorkspace
)

// Tenant-scoped permission codes.
const (
	PermTenantMemberRead      = catalog.PermTenantMemberRead
	PermTenantMemberWrite     = catalog.PermTenantMemberWrite
	PermTenantRoleRead        = catalog.PermTenantRoleRead
	PermTenantRoleWrite       = catalog.PermTenantRoleWrite
	PermTenantWorkspaceCreate = catalog.PermTenantWorkspaceCreate
	PermTenantWorkspaceJoin   = catalog.PermTenantWorkspaceJoin
	PermTenantAgentUse        = catalog.PermTenantAgentUse
)

// Workspace-scoped permission codes.
const (
	PermWorkspaceRead           = catalog.PermWorkspaceRead
	PermWorkspaceDelete         = catalog.PermWorkspaceDelete
	PermWorkspaceMemberRead     = catalog.PermWorkspaceMemberRead
	PermWorkspaceMemberWrite    = catalog.PermWorkspaceMemberWrite
	PermWorkspaceGitBind        = catalog.PermWorkspaceGitBind
	PermWorkspaceGitPush        = catalog.PermWorkspaceGitPush
	PermWorkspaceEnvRead        = catalog.PermWorkspaceEnvRead
	PermWorkspaceEnvCreate      = catalog.PermWorkspaceEnvCreate
	PermWorkspaceEnvDelete      = catalog.PermWorkspaceEnvDelete
	PermWorkspaceEnvPromote     = catalog.PermWorkspaceEnvPromote
	PermWorkspaceCodeRead       = catalog.PermWorkspaceCodeRead
	PermWorkspaceCodeWrite      = catalog.PermWorkspaceCodeWrite
	PermWorkspacePlanPreview    = catalog.PermWorkspacePlanPreview
	PermWorkspacePlanApply      = catalog.PermWorkspacePlanApply
	PermWorkspaceQueryRun       = catalog.PermWorkspaceQueryRun
	PermWorkspaceKnowledgeRead  = catalog.PermWorkspaceKnowledgeRead
	PermWorkspaceKnowledgeWrite = catalog.PermWorkspaceKnowledgeWrite
	PermWorkspaceOntologyRead   = catalog.PermWorkspaceOntologyRead
	PermWorkspacePipelineRead   = catalog.PermWorkspacePipelineRead
	PermWorkspacePipelineRun    = catalog.PermWorkspacePipelineRun
	PermWorkspaceLogsRead       = catalog.PermWorkspaceLogsRead
)

// Built-in role codes, persisted in tenant_members.role and
// workspace_members.role_code.
const (
	RoleTenantAdmin     = catalog.RoleTenantAdmin
	RoleTenantDeveloper = catalog.RoleTenantDeveloper
	RoleTenantViewer    = catalog.RoleTenantViewer

	RoleWorkspaceOwner    = catalog.RoleWorkspaceOwner
	RoleWorkspaceEngineer = catalog.RoleWorkspaceEngineer
	RoleWorkspaceAnalyst  = catalog.RoleWorkspaceAnalyst
	RoleWorkspaceViewer   = catalog.RoleWorkspaceViewer
)

// Catalog is the authoritative permission list this service synchronizes into
// the permissions table.
var Catalog = catalog.Catalog

// BuiltinRoleTemplates are cloned into every tenant by the seeder.
var BuiltinRoleTemplates = catalog.BuiltinRoleTemplates

// IsCatalogPermission reports whether code is a registered permission point.
// Role composition endpoints reject anything else, which is what keeps an
// unreachable permission out of the database.
func IsCatalogPermission(code string) bool { return catalog.IsCatalogPermission(code) }

// ScopeOf returns the scope a permission code belongs to.
func ScopeOf(code string) (Scope, bool) { return catalog.ScopeOf(code) }

// IsValidScope reports whether value is a supported scope name.
func IsValidScope(value string) bool { return catalog.IsValidScope(value) }
