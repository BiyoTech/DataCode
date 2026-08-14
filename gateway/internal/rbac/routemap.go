package rbac

import (
	"net/http"
	"strings"
)

// ScopeSource tells the authorization middleware where the scope of a request
// is found. Resolution happens before chi matches the route, so path segments
// are read from the raw URL instead of chi.URLParam.
type ScopeSource int

const (
	// ScopeSourceNone requires nothing beyond a valid identity.
	ScopeSourceNone ScopeSource = iota
	// ScopeSourceTenant checks the permission against the caller's tenant role.
	ScopeSourceTenant
	// ScopeSourceWorkspaceSegment reads the workspace id from a path segment.
	ScopeSourceWorkspaceSegment
	// ScopeSourceWorkspaceField reads "workspace_id" from the query or JSON body.
	ScopeSourceWorkspaceField
	// ScopeSourceWorkspacePath reads "workspace_path" from the query or JSON
	// body and derives the workspace id from the working-copy layout.
	ScopeSourceWorkspacePath
)

// Rule maps a request onto the permission it requires.
type Rule struct {
	// Method is the HTTP method; an empty value matches any method.
	Method string
	// Pattern is an absolute path pattern. A "{}" segment matches exactly one
	// segment and a trailing "*" matches the remaining segments.
	Pattern string
	// Permission is the required permission code. Empty means "authenticated
	// identity is enough".
	Permission string
	// Source declares where the scope of the request is found.
	Source ScopeSource
	// SegmentIndex is the zero-based position of the workspace id when Source
	// is ScopeSourceWorkspaceSegment.
	SegmentIndex int
	// ScopeOptional keeps a route reachable when no workspace can be resolved
	// from the request (e.g. instance-wide reads). The permission is then
	// checked against every workspace the caller is a member of and granted if
	// at least one of them allows it, instead of being silently skipped.
	ScopeOptional bool
	// Deny marks a request that must be rejected outright.
	Deny bool
}

// rules is evaluated in declaration order and the first match wins, so specific
// entries must precede the per-group fallbacks.
//
// Every request that reaches the gateway is covered: unmatched read methods
// degrade to "authenticated only" (keeping new backend read endpoints available
// without a gateway change) while unmatched state-changing methods are denied.
var rules = []Rule{
	// ---------------------------------------------------------------- health
	{Method: http.MethodGet, Pattern: "/health", Source: ScopeSourceNone},

	// ------------------------------------------------ identity & tenant members
	{Method: http.MethodGet, Pattern: "/api/v1/auth/members",
		Permission: PermTenantMemberRead, Source: ScopeSourceTenant},
	{Method: http.MethodPost, Pattern: "/api/v1/auth/members",
		Permission: PermTenantMemberWrite, Source: ScopeSourceTenant},
	{Method: http.MethodPatch, Pattern: "/api/v1/auth/members/{}",
		Permission: PermTenantMemberWrite, Source: ScopeSourceTenant},
	{Method: http.MethodDelete, Pattern: "/api/v1/auth/members/{}",
		Permission: PermTenantMemberWrite, Source: ScopeSourceTenant},
	// Session lifecycle and the member picker directory are available to every
	// authenticated identity.
	{Pattern: "/api/v1/auth/*", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/me", Source: ScopeSourceNone},

	// ------------------------------------------------------- role management
	{Method: http.MethodGet, Pattern: "/api/v1/rbac/permissions",
		Permission: PermTenantRoleRead, Source: ScopeSourceTenant},
	{Method: http.MethodGet, Pattern: "/api/v1/rbac/roles",
		Permission: PermTenantRoleRead, Source: ScopeSourceTenant},
	{Method: http.MethodPost, Pattern: "/api/v1/rbac/roles",
		Permission: PermTenantRoleWrite, Source: ScopeSourceTenant},
	{Method: http.MethodPut, Pattern: "/api/v1/rbac/roles/{}",
		Permission: PermTenantRoleWrite, Source: ScopeSourceTenant},
	{Method: http.MethodDelete, Pattern: "/api/v1/rbac/roles/{}",
		Permission: PermTenantRoleWrite, Source: ScopeSourceTenant},

	// ------------------------------------------------------------ workspaces
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces",
		Permission: PermTenantWorkspaceCreate, Source: ScopeSourceTenant},
	// The listing is already restricted to the caller's own bindings.
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces", Source: ScopeSourceNone},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/bind-git",
		Permission: PermWorkspaceGitBind, Source: ScopeSourceWorkspaceField},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/sync",
		Permission: PermWorkspaceGitPush, Source: ScopeSourceWorkspaceField},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/{}/open",
		Permission: PermWorkspaceRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces/{}/remote-branches",
		Permission: PermWorkspaceRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces/{}/members",
		Permission: PermWorkspaceMemberRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/{}/members",
		Permission: PermWorkspaceMemberWrite, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodDelete, Pattern: "/api/v1/workspaces/{}/members/{}",
		Permission: PermWorkspaceMemberWrite, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodDelete, Pattern: "/api/v1/workspaces/{}",
		Permission: PermWorkspaceDelete, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},

	// ------------------------------------------------------- agent & models
	{Pattern: "/api/v1/sessions/*", Permission: PermTenantAgentUse, Source: ScopeSourceTenant},
	{Method: http.MethodGet, Pattern: "/api/v1/models",
		Permission: PermTenantAgentUse, Source: ScopeSourceTenant},

	// --------------------------------------------------------------- sqlmesh
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/plan",
		Permission: PermWorkspacePlanPreview, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/apply",
		Permission: PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/apply/async",
		Permission: PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/cancel",
		Permission: PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/query",
		Permission: PermWorkspaceQueryRun, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/evaluate",
		Permission: PermWorkspaceQueryRun, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/render",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/validate",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/schema/discover",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	// Static service metadata carries no workspace context.
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/meta", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/modules", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/*",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/sqlmesh/*",
		Permission: PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------- sqlmesh identity-path routes
	{Method: http.MethodPost, Pattern: "/api/v1/external-models/import",
		Permission: PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodGet, Pattern: "/api/v1/gateways",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodGet, Pattern: "/api/v1/gateways/*",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodGet, Pattern: "/api/v1/schemas",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},

	// ------------------------------------------------------------------- git
	// Git credentials belong to the individual user, not to a workspace.
	{Pattern: "/api/v1/git/credentials", Source: ScopeSourceNone},
	{Pattern: "/api/v1/git/credentials/*", Source: ScopeSourceNone},
	{Method: http.MethodPost, Pattern: "/api/v1/git/push",
		Permission: PermWorkspaceGitPush, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/git/publish",
		Permission: PermWorkspaceGitPush, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodGet, Pattern: "/api/v1/git/*",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/git/*",
		Permission: PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ----------------------------------------------------------------- files
	{Method: http.MethodGet, Pattern: "/api/v1/files/*",
		Permission: PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/files/*",
		Permission: PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------------------------- knowledge
	{Method: http.MethodGet, Pattern: "/api/v1/knowledge/*",
		Permission: PermWorkspaceKnowledgeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/knowledge/*",
		// Uploads are multipart, so the workspace cannot always be resolved
		// from the body; the permission is then required in some workspace.
		Permission: PermWorkspaceKnowledgeWrite, Source: ScopeSourceWorkspacePath, ScopeOptional: true},

	// -------------------------------------------------------------- ontology
	{Method: http.MethodGet, Pattern: "/api/v1/ontology/*",
		Permission: PermWorkspaceOntologyRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/ontology/*",
		Permission: PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// --------------------------------------------------------------- dagster
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/health", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/mode", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/*",
		Permission: PermWorkspacePipelineRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/dagster/*",
		// Run cancellation is addressed by run id only, hence ScopeOptional.
		Permission: PermWorkspacePipelineRun, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
}

// MatchRoute resolves the rule guarding a request. When no rule matches, read
// methods fall back to "authenticated only" and state-changing methods are
// denied, so a new backend write endpoint can never be exposed by accident.
func MatchRoute(method, requestPath string) Rule {
	segments := splitPath(requestPath)
	for _, rule := range rules {
		if rule.Method != "" && rule.Method != method {
			continue
		}
		if matchPattern(rule.Pattern, segments) {
			return rule
		}
	}
	switch method {
	case http.MethodGet, http.MethodHead, http.MethodOptions:
		return Rule{Source: ScopeSourceNone}
	default:
		return Rule{Deny: true}
	}
}

// splitPath normalizes a request path into non-empty segments.
func splitPath(requestPath string) []string {
	trimmed := strings.Trim(requestPath, "/")
	if trimmed == "" {
		return nil
	}
	return strings.Split(trimmed, "/")
}

// matchPattern compares a pattern against path segments. "{}" matches exactly
// one segment; a trailing "*" matches the remaining segments (including none).
func matchPattern(pattern string, segments []string) bool {
	patternSegments := splitPath(pattern)
	for i, want := range patternSegments {
		if want == "*" {
			return true
		}
		if i >= len(segments) {
			return false
		}
		if want == "{}" || want == segments[i] {
			continue
		}
		return false
	}
	return len(patternSegments) == len(segments)
}

// SegmentAt returns the path segment at index, if present.
func SegmentAt(requestPath string, index int) (string, bool) {
	segments := splitPath(requestPath)
	if index < 0 || index >= len(segments) {
		return "", false
	}
	return segments[index], true
}
