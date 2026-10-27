// Package rbac holds the gateway's authorization DECISION vocabulary.
//
// It is STATELESS by design. It used to own the RBAC tables and answer decisions
// from the database; identity-service is the single source of truth for that
// data since P2, and keeping a second writer here blocked the P7 Global/Cell
// database split. What remains is the part that must live where requests are
// enforced:
//
//   - the ROUTE MAP (this file) — which permission each route requires and where
//     its scope is found;
//   - workspace path parsing (wspath.go) — deriving the scope of a request.
//
// The permission CATALOG itself lives in the shared module
// (github.com/datacode/shared/rbac, imported here as "catalog"): the gateway and
// identity-service must agree on the vocabulary, and two hand-written copies is
// how a route ends up requiring a permission that no role can hold. The data
// decisions are taken against arrives as an authorization snapshot
// (internal/authz), not from a query.
package rbac

import (
	"net/http"
	"strings"

	catalog "github.com/datacode/shared/rbac"
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
	// ScopeSourceEnvironmentSegment reads an environment id ("{workspace_id}:
	// {environment}") from a path segment and derives the workspace from it.
	ScopeSourceEnvironmentSegment
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
		Permission: catalog.PermTenantMemberRead, Source: ScopeSourceTenant},
	{Method: http.MethodPost, Pattern: "/api/v1/auth/members",
		Permission: catalog.PermTenantMemberWrite, Source: ScopeSourceTenant},
	{Method: http.MethodPatch, Pattern: "/api/v1/auth/members/{}",
		Permission: catalog.PermTenantMemberWrite, Source: ScopeSourceTenant},
	{Method: http.MethodDelete, Pattern: "/api/v1/auth/members/{}",
		Permission: catalog.PermTenantMemberWrite, Source: ScopeSourceTenant},
	// Session lifecycle and the member picker directory are available to every
	// authenticated identity.
	{Pattern: "/api/v1/auth/*", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/me", Source: ScopeSourceNone},

	// ------------------------------------------------------- role management
	{Method: http.MethodGet, Pattern: "/api/v1/rbac/permissions",
		Permission: catalog.PermTenantRoleRead, Source: ScopeSourceTenant},
	{Method: http.MethodGet, Pattern: "/api/v1/rbac/roles",
		Permission: catalog.PermTenantRoleRead, Source: ScopeSourceTenant},
	{Method: http.MethodPost, Pattern: "/api/v1/rbac/roles",
		Permission: catalog.PermTenantRoleWrite, Source: ScopeSourceTenant},
	{Method: http.MethodPut, Pattern: "/api/v1/rbac/roles/{}",
		Permission: catalog.PermTenantRoleWrite, Source: ScopeSourceTenant},
	{Method: http.MethodDelete, Pattern: "/api/v1/rbac/roles/{}",
		Permission: catalog.PermTenantRoleWrite, Source: ScopeSourceTenant},

	// ------------------------------------------------------------ workspaces
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces",
		Permission: catalog.PermTenantWorkspaceCreate, Source: ScopeSourceTenant},
	// The listing is already restricted to the caller's own bindings.
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces", Source: ScopeSourceNone},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/bind-git",
		Permission: catalog.PermWorkspaceGitBind, Source: ScopeSourceWorkspaceField},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/sync",
		Permission: catalog.PermWorkspaceGitPush, Source: ScopeSourceWorkspaceField},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/{}/open",
		Permission: catalog.PermWorkspaceRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces/{}/remote-branches",
		Permission: catalog.PermWorkspaceRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces/{}/members",
		Permission: catalog.PermWorkspaceMemberRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/{}/members",
		Permission: catalog.PermWorkspaceMemberWrite, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodDelete, Pattern: "/api/v1/workspaces/{}/members/{}",
		Permission: catalog.PermWorkspaceMemberWrite, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodDelete, Pattern: "/api/v1/workspaces/{}",
		Permission: catalog.PermWorkspaceDelete, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},

	// ---------------------------------------------------------- environments
	// The environment registry is the platform's development-line API: it owns
	// the Git branch, the SQLMesh virtual environment and the Dagster
	// deployment as one unit.
	{Method: http.MethodGet, Pattern: "/api/v1/workspaces/{}/environments",
		Permission: catalog.PermWorkspaceEnvRead, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	{Method: http.MethodPost, Pattern: "/api/v1/workspaces/{}/environments",
		Permission: catalog.PermWorkspaceEnvCreate, Source: ScopeSourceWorkspaceSegment, SegmentIndex: 3},
	// Materializing a working copy of an existing environment is an ordinary
	// read of the workspace: it creates no shared resource.
	{Method: http.MethodPost, Pattern: "/api/v1/environments/{}/open",
		Permission: catalog.PermWorkspaceRead, Source: ScopeSourceEnvironmentSegment, SegmentIndex: 3},
	// Reconciliation may recreate a branch and a virtual environment, which is
	// what creating an environment does — hence env:create, not env:read.
	{Method: http.MethodPost, Pattern: "/api/v1/environments/{}/reconcile",
		Permission: catalog.PermWorkspaceEnvCreate, Source: ScopeSourceEnvironmentSegment, SegmentIndex: 3},
	{Method: http.MethodDelete, Pattern: "/api/v1/environments/{}",
		Permission: catalog.PermWorkspaceEnvDelete, Source: ScopeSourceEnvironmentSegment, SegmentIndex: 3},
	// Details, events and any future read (diff, drift) share one rule so a new
	// backend read endpoint cannot degrade to "authenticated only".
	{Method: http.MethodGet, Pattern: "/api/v1/environments/*",
		Permission: catalog.PermWorkspaceEnvRead, Source: ScopeSourceEnvironmentSegment, SegmentIndex: 3},

	// ---------------------------------------------------------------- quotas
	// Authenticated identity is enough: the endpoint reports the CALLER'S OWN
	// tenant, resolved from the identity header, so there is nothing to
	// authorize a scope against. Only GET is listed, which leaves any future
	// write (an admin raising a limit) to be denied by the fallback until it is
	// declared here deliberately.
	{Method: http.MethodGet, Pattern: "/api/v1/quotas", Source: ScopeSourceTenant},

	// ------------------------------------------------------- agent & models
	{Pattern: "/api/v1/sessions/*", Permission: catalog.PermTenantAgentUse, Source: ScopeSourceTenant},
	{Method: http.MethodGet, Pattern: "/api/v1/models",
		Permission: catalog.PermTenantAgentUse, Source: ScopeSourceTenant},

	// --------------------------------------------------------------- sqlmesh
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/plan",
		Permission: catalog.PermWorkspacePlanPreview, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/apply",
		Permission: catalog.PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/apply/async",
		Permission: catalog.PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/cancel",
		Permission: catalog.PermWorkspacePlanApply, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/query",
		Permission: catalog.PermWorkspaceQueryRun, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/evaluate",
		Permission: catalog.PermWorkspaceQueryRun, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/render",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/validate",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/schema/discover",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	// Repairing the platform-managed state_schema rewrites config.yaml, i.e. a
	// code change, so it is gated like any other file write.
	{Method: http.MethodPost, Pattern: "/api/v1/sqlmesh/config/state-schema/repair",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},
	// Static service metadata carries no workspace context.
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/meta", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/modules", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/sqlmesh/*",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/sqlmesh/*",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------- sqlmesh identity-path routes
	{Method: http.MethodPost, Pattern: "/api/v1/external-models/import",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodGet, Pattern: "/api/v1/gateways",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodGet, Pattern: "/api/v1/gateways/*",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodGet, Pattern: "/api/v1/schemas",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},

	// ------------------------------------------------------------------- git
	// Git credentials belong to the individual user, not to a workspace.
	{Pattern: "/api/v1/git/credentials", Source: ScopeSourceNone},
	{Pattern: "/api/v1/git/credentials/*", Source: ScopeSourceNone},
	{Method: http.MethodPost, Pattern: "/api/v1/git/push",
		Permission: catalog.PermWorkspaceGitPush, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/git/publish",
		Permission: catalog.PermWorkspaceGitPush, Source: ScopeSourceWorkspacePath},
	// Merging one environment branch into another publishes the result on the
	// remote, so it is gated like any other push. Merging into the PRODUCTION
	// branch additionally requires workspace:env:promote, which the service
	// checks from the granted permission set: the target branch lives in the
	// body, and the route map is intentionally static.
	{Method: http.MethodPost, Pattern: "/api/v1/git/merge",
		Permission: catalog.PermWorkspaceGitPush, Source: ScopeSourceWorkspacePath},
	// Syncing a base branch into the caller's own working copy changes files
	// only locally — an ordinary code write, no remote mutation.
	{Method: http.MethodPost, Pattern: "/api/v1/git/sync",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodGet, Pattern: "/api/v1/git/*",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/git/*",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ----------------------------------------------------------------- files
	{Method: http.MethodGet, Pattern: "/api/v1/files/*",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/files/*",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------------------------------ deps
	// Working-copy Python dependencies. Reads only — installing happens
	// through the reload route, which is already gated as a code write.
	//
	// /baseline describes the runtime IMAGE, not any workspace, so it resolves
	// no scope and carries no permission (ScopeSourceNone ignores one): it is
	// the list a user needs in order to write a declaration that can install at
	// all, every working copy's scaffolded pyproject.toml already quotes part
	// of it, and requiring a workspace to read it would make the answer look
	// workspace-specific when it is identical everywhere. Authenticated is
	// enough, matching /sqlmesh/meta and /dagster/mode.
	{Method: http.MethodGet, Pattern: "/api/v1/deps/baseline", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/deps/status",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------------------------------- wip
	// The durability-contract surface (design 07 Q6): reading one working
	// copy's protection state is a code read; recovering or discarding a
	// snapshot rewrites the working copy / the Git server's hidden ref, which
	// is a code write.
	{Method: http.MethodGet, Pattern: "/api/v1/wip/status",
		Permission: catalog.PermWorkspaceCodeRead, Source: ScopeSourceWorkspacePath},
	{Pattern: "/api/v1/wip/*",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------------------------------ logs
	// Read-only, and deliberately NOT ScopeOptional: a log query without a
	// resolvable workspace has no meaning, so an unresolvable one must be
	// rejected rather than degraded into "has the permission somewhere".
	{Method: http.MethodGet, Pattern: "/api/v1/logs",
		Permission: catalog.PermWorkspaceLogsRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodGet, Pattern: "/api/v1/logs/*",
		Permission: catalog.PermWorkspaceLogsRead, Source: ScopeSourceWorkspacePath},

	// ------------------------------------------------------------- knowledge
	{Method: http.MethodGet, Pattern: "/api/v1/knowledge/*",
		Permission: catalog.PermWorkspaceKnowledgeRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/knowledge/*",
		// Uploads are multipart, so the workspace cannot always be resolved
		// from the body; the permission is then required in some workspace.
		Permission: catalog.PermWorkspaceKnowledgeWrite, Source: ScopeSourceWorkspacePath, ScopeOptional: true},

	// -------------------------------------------------------------- ontology
	{Method: http.MethodGet, Pattern: "/api/v1/ontology/*",
		Permission: catalog.PermWorkspaceOntologyRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/ontology/*",
		Permission: catalog.PermWorkspaceCodeWrite, Source: ScopeSourceWorkspacePath},

	// --------------------------------------------------------------- dagster
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/health", Source: ScopeSourceNone},
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/mode", Source: ScopeSourceNone},
	// Deployments are the Dagster side of an environment. Bringing one up or
	// refreshing it is an orchestration action on a known working copy, so the
	// workspace MUST resolve from the body — unlike run cancellation, there is
	// no id-only form of these calls.
	{Method: http.MethodPost, Pattern: "/api/v1/dagster/deployments/ensure",
		Permission: catalog.PermWorkspacePipelineRun, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/dagster/deployments/reload",
		Permission: catalog.PermWorkspacePipelineRun, Source: ScopeSourceWorkspacePath},
	// A reload is asynchronous: reading its progress is a READ on the same
	// working copy, so it takes pipeline:read rather than pipeline:run — a user
	// who may look at a pipeline may watch a reload someone else triggered.
	//
	// The scope is NOT optional, unlike the catch-all GET rule below. The runtime
	// is a headless Service and this request must reach the replica holding the
	// copy's lease; a query with no workspace_path would be answered by whichever
	// replica the DNS round-robin picked, which knows nothing about the job.
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/deployments/reload/status",
		Permission: catalog.PermWorkspacePipelineRead, Source: ScopeSourceWorkspacePath},
	{Method: http.MethodPost, Pattern: "/api/v1/dagster/deployments/reload/cancel",
		Permission: catalog.PermWorkspacePipelineRun, Source: ScopeSourceWorkspacePath},
	// Tearing a deployment down is part of destroying an environment, and the
	// request carries only the deployment name — hence env:delete in some
	// workspace rather than pipeline:run on a resolvable one.
	{Method: http.MethodDelete, Pattern: "/api/v1/dagster/deployments/*",
		Permission: catalog.PermWorkspaceEnvDelete, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Method: http.MethodGet, Pattern: "/api/v1/dagster/*",
		Permission: catalog.PermWorkspacePipelineRead, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
	{Pattern: "/api/v1/dagster/*",
		// Run cancellation is addressed by run id only, hence ScopeOptional.
		Permission: catalog.PermWorkspacePipelineRun, Source: ScopeSourceWorkspacePath, ScopeOptional: true},
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
