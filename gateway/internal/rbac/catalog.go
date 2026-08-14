// Package rbac implements the platform's database-driven RBAC model.
//
// Responsibility split:
//   - The permission CATALOG (which permission codes exist at all) is declared
//     in code and synchronized into the database on startup. It is never
//     mutated through the API, so a permission code can never be invented by
//     writing to the database.
//   - ROLES and their permission composition live entirely in the database and
//     are tenant-editable, including the built-in ones.
//
// Roles are stored in two flavours:
//   - system templates (tenant_id == "") — written only by the seeder;
//   - tenant instances (tenant_id == "<tid>") — cloned from the templates the
//     first time a tenant is resolved and never touched by the seeder again.
//
// That separation is what allows a tenant to edit the permission set of a
// built-in role without the change being reverted on the next startup.
package rbac

// Scope identifies the authorization scope a permission or role belongs to.
// New scopes (e.g. "org", "project") can be added without schema changes.
type Scope string

const (
	ScopeTenant    Scope = "tenant"
	ScopeWorkspace Scope = "workspace"
)

// Tenant-scoped permission codes.
const (
	PermTenantMemberRead      = "tenant:member:read"
	PermTenantMemberWrite     = "tenant:member:write"
	PermTenantRoleRead        = "tenant:role:read"
	PermTenantRoleWrite       = "tenant:role:write"
	PermTenantWorkspaceCreate = "tenant:workspace:create"
	// PermTenantWorkspaceJoin marks accounts that may be granted access to a
	// workspace. Members without it never appear in a workspace member picker.
	PermTenantWorkspaceJoin = "tenant:workspace:join"
	PermTenantAgentUse      = "tenant:agent:use"
)

// Workspace-scoped permission codes.
const (
	PermWorkspaceRead           = "workspace:read"
	PermWorkspaceDelete         = "workspace:delete"
	PermWorkspaceMemberRead     = "workspace:member:read"
	PermWorkspaceMemberWrite    = "workspace:member:write"
	PermWorkspaceGitBind        = "workspace:git:bind"
	PermWorkspaceGitPush        = "workspace:git:push"
	PermWorkspaceCodeRead       = "workspace:code:read"
	PermWorkspaceCodeWrite      = "workspace:code:write"
	PermWorkspacePlanPreview    = "workspace:plan:preview"
	PermWorkspacePlanApply      = "workspace:plan:apply"
	PermWorkspaceQueryRun       = "workspace:query:run"
	PermWorkspaceKnowledgeRead  = "workspace:knowledge:read"
	PermWorkspaceKnowledgeWrite = "workspace:knowledge:write"
	PermWorkspaceOntologyRead   = "workspace:ontology:read"
	PermWorkspacePipelineRead   = "workspace:pipeline:read"
	PermWorkspacePipelineRun    = "workspace:pipeline:run"
)

// Built-in role codes. The literals are kept stable across versions because
// they are persisted in tenant_members.role and workspace_members.role_code.
const (
	RoleTenantAdmin     = "admin"
	RoleTenantDeveloper = "developer"
	RoleTenantViewer    = "viewer"

	RoleWorkspaceOwner    = "workspace_owner"
	RoleWorkspaceEngineer = "engineer"
	RoleWorkspaceAnalyst  = "analyst"
	RoleWorkspaceViewer   = "viewer"
)

// PermissionDef is one entry of the code-side permission catalog.
type PermissionDef struct {
	Code        string `json:"code"`
	Scope       Scope  `json:"scope"`
	Resource    string `json:"resource"`
	Action      string `json:"action"`
	Description string `json:"description"`
}

// Catalog is the authoritative list of permission codes known to the platform.
// Adding a permission point means adding an entry here; the startup sync makes
// it selectable when composing roles.
var Catalog = []PermissionDef{
	// Tenant scope.
	{PermTenantMemberRead, ScopeTenant, "member", "read", "View tenant members"},
	{PermTenantMemberWrite, ScopeTenant, "member", "write", "Add, update and remove tenant members"},
	{PermTenantRoleRead, ScopeTenant, "role", "read", "View roles and their permissions"},
	{PermTenantRoleWrite, ScopeTenant, "role", "write", "Create, update and delete roles"},
	{PermTenantWorkspaceCreate, ScopeTenant, "workspace", "create", "Create workspaces inside the tenant"},
	{PermTenantWorkspaceJoin, ScopeTenant, "workspace", "join", "Can be granted access to workspaces"},
	{PermTenantAgentUse, ScopeTenant, "agent", "use", "Use agent sessions and language models"},

	// Workspace scope.
	{PermWorkspaceRead, ScopeWorkspace, "workspace", "read", "Open the workspace and read its metadata"},
	{PermWorkspaceDelete, ScopeWorkspace, "workspace", "delete", "Delete the workspace"},
	{PermWorkspaceMemberRead, ScopeWorkspace, "member", "read", "View workspace members"},
	{PermWorkspaceMemberWrite, ScopeWorkspace, "member", "write", "Grant and revoke workspace access"},
	{PermWorkspaceGitBind, ScopeWorkspace, "git", "bind", "Bind or rebind the Git repository"},
	{PermWorkspaceGitPush, ScopeWorkspace, "git", "push", "Push commits to the remote repository"},
	{PermWorkspaceCodeRead, ScopeWorkspace, "code", "read", "Read models, files and lineage"},
	{PermWorkspaceCodeWrite, ScopeWorkspace, "code", "write", "Create, modify and delete files"},
	{PermWorkspacePlanPreview, ScopeWorkspace, "plan", "preview", "Run plan previews without applying"},
	{PermWorkspacePlanApply, ScopeWorkspace, "plan", "apply", "Apply plans to an environment"},
	{PermWorkspaceQueryRun, ScopeWorkspace, "query", "run", "Execute ad-hoc queries"},
	{PermWorkspaceKnowledgeRead, ScopeWorkspace, "knowledge", "read", "Read business knowledge documents"},
	{PermWorkspaceKnowledgeWrite, ScopeWorkspace, "knowledge", "write", "Create and delete knowledge documents"},
	{PermWorkspaceOntologyRead, ScopeWorkspace, "ontology", "read", "Read the semantic ontology"},
	{PermWorkspacePipelineRead, ScopeWorkspace, "pipeline", "read", "View pipelines, assets and runs"},
	{PermWorkspacePipelineRun, ScopeWorkspace, "pipeline", "run", "Trigger and cancel pipeline runs"},
}

// RoleTemplate describes a built-in role and its default permission set.
type RoleTemplate struct {
	Code        string
	Scope       Scope
	Name        string
	Description string
	Permissions []string
}

// BuiltinRoleTemplates are cloned into every tenant. Cloned rows are marked as
// built-in: they cannot be deleted and their code cannot change, but their name
// and permission set are editable by the tenant.
var BuiltinRoleTemplates = []RoleTemplate{
	{
		Code:        RoleTenantAdmin,
		Scope:       ScopeTenant,
		Name:        "Administrator",
		Description: "Full control over tenant members and roles",
		Permissions: []string{
			PermTenantMemberRead, PermTenantMemberWrite,
			PermTenantRoleRead, PermTenantRoleWrite,
			PermTenantWorkspaceCreate, PermTenantWorkspaceJoin, PermTenantAgentUse,
		},
	},
	{
		Code:        RoleTenantDeveloper,
		Scope:       ScopeTenant,
		Name:        "Developer",
		Description: "Can create workspaces and be granted workspace access",
		Permissions: []string{
			PermTenantMemberRead, PermTenantRoleRead,
			PermTenantWorkspaceCreate, PermTenantWorkspaceJoin, PermTenantAgentUse,
		},
	},
	{
		Code:        RoleTenantViewer,
		Scope:       ScopeTenant,
		Name:        "Read-only member",
		Description: "Read-only observer of tenant level resources",
		Permissions: []string{PermTenantMemberRead},
	},
	{
		Code:        RoleWorkspaceOwner,
		Scope:       ScopeWorkspace,
		Name:        "Owner",
		Description: "Workspace creator with full control; granted implicitly",
		Permissions: allWorkspacePermissions(),
	},
	{
		Code:        RoleWorkspaceEngineer,
		Scope:       ScopeWorkspace,
		Name:        "Engineer",
		Description: "Writes models and code, pushes and applies plans",
		Permissions: []string{
			PermWorkspaceRead, PermWorkspaceMemberRead,
			PermWorkspaceCodeRead, PermWorkspaceCodeWrite, PermWorkspaceGitPush,
			PermWorkspacePlanPreview, PermWorkspacePlanApply, PermWorkspaceQueryRun,
			PermWorkspaceKnowledgeRead, PermWorkspaceKnowledgeWrite,
			PermWorkspaceOntologyRead, PermWorkspacePipelineRead, PermWorkspacePipelineRun,
		},
	},
	{
		Code:        RoleWorkspaceAnalyst,
		Scope:       ScopeWorkspace,
		Name:        "Analyst",
		Description: "Explores data and previews plans without writing code",
		Permissions: []string{
			PermWorkspaceRead, PermWorkspaceMemberRead,
			PermWorkspaceCodeRead, PermWorkspacePlanPreview, PermWorkspaceQueryRun,
			PermWorkspaceKnowledgeRead, PermWorkspaceOntologyRead, PermWorkspacePipelineRead,
		},
	},
	{
		Code:        RoleWorkspaceViewer,
		Scope:       ScopeWorkspace,
		Name:        "Read-only",
		Description: "Read-only access to the workspace",
		Permissions: []string{
			PermWorkspaceRead, PermWorkspaceMemberRead, PermWorkspaceCodeRead,
			PermWorkspaceKnowledgeRead, PermWorkspaceOntologyRead, PermWorkspacePipelineRead,
		},
	},
}

// allWorkspacePermissions returns every workspace-scoped code in the catalog,
// so the owner template stays complete when new permissions are introduced.
func allWorkspacePermissions() []string {
	codes := make([]string, 0, len(Catalog))
	for _, def := range Catalog {
		if def.Scope == ScopeWorkspace {
			codes = append(codes, def.Code)
		}
	}
	return codes
}

// IsCatalogPermission reports whether code is a registered permission point.
// Role composition endpoints must reject anything else.
func IsCatalogPermission(code string) bool {
	for _, def := range Catalog {
		if def.Code == code {
			return true
		}
	}
	return false
}

// ScopeOf returns the scope a permission code belongs to.
func ScopeOf(code string) (Scope, bool) {
	for _, def := range Catalog {
		if def.Code == code {
			return def.Scope, true
		}
	}
	return "", false
}

// IsValidScope reports whether value is a supported scope name.
func IsValidScope(value string) bool {
	switch Scope(value) {
	case ScopeTenant, ScopeWorkspace:
		return true
	default:
		return false
	}
}
