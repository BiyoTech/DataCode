package rbac

import (
	"net/http"
	"testing"

	catalog "github.com/datacode/shared/rbac"
)

// TestWipRoutesAreWorkspaceScoped pins the durability-contract surface (design
// 07 Q6): the status read is a code read, while recover/discard rewrite the
// working copy or the Git server's hidden ref — a code write. Both must be
// resolvable to a workspace, never degradable to "has the permission somewhere".
func TestWipRoutesAreWorkspaceScoped(t *testing.T) {
	rule := MatchRoute(http.MethodGet, "/api/v1/wip/status")
	if rule.Permission != catalog.PermWorkspaceCodeRead ||
		rule.Source != ScopeSourceWorkspacePath || rule.ScopeOptional {
		t.Errorf("wip status = %+v, want a non-optional workspace code read", rule)
	}

	for _, path := range []string{
		"/api/v1/wip/recover", "/api/v1/wip/discard",
	} {
		rule := MatchRoute(http.MethodPost, path)
		if rule.Permission != catalog.PermWorkspaceCodeWrite ||
			rule.Source != ScopeSourceWorkspacePath || rule.ScopeOptional {
			t.Errorf("%s = %+v, want a non-optional workspace code write", path, rule)
		}
	}
}

// TestMatchRouteSpecificBeforeFallback verifies the declaration-order contract:
// a specific rule must win over the per-group fallback.
func TestMatchRouteSpecificBeforeFallback(t *testing.T) {
	cases := []struct {
		name           string
		method         string
		path           string
		wantPermission string
		wantSource     ScopeSource
	}{
		{
			name:           "tenant member write",
			method:         http.MethodPost,
			path:           "/api/v1/auth/members",
			wantPermission: catalog.PermTenantMemberWrite,
			wantSource:     ScopeSourceTenant,
		},
		{
			// Repairing state_schema rewrites config.yaml: gated as a code
			// write and scoped by workspace_path in the body.
			name:           "state schema repair is a code write",
			method:         http.MethodPost,
			path:           "/api/v1/sqlmesh/config/state-schema/repair",
			wantPermission: catalog.PermWorkspaceCodeWrite,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "session lifecycle needs no permission",
			method:         http.MethodPost,
			path:           "/api/v1/auth/logout",
			wantPermission: "",
			wantSource:     ScopeSourceNone,
		},
		{
			name:           "bind-git wins over the workspace id segment rule",
			method:         http.MethodPost,
			path:           "/api/v1/workspaces/bind-git",
			wantPermission: catalog.PermWorkspaceGitBind,
			wantSource:     ScopeSourceWorkspaceField,
		},
		{
			name:           "workspace member revocation",
			method:         http.MethodDelete,
			path:           "/api/v1/workspaces/ws-t1-demo/members/u2",
			wantPermission: catalog.PermWorkspaceMemberWrite,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "environment creation is separated from workspace writes",
			method:         http.MethodPost,
			path:           "/api/v1/workspaces/ws-t1-demo/environments",
			wantPermission: catalog.PermWorkspaceEnvCreate,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "environment list only needs the read permission",
			method:         http.MethodGet,
			path:           "/api/v1/workspaces/ws-t1-demo/environments",
			wantPermission: catalog.PermWorkspaceEnvRead,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "opening a working copy is an ordinary workspace read",
			method:         http.MethodPost,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/open",
			wantPermission: catalog.PermWorkspaceRead,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "environment destruction needs the delete permission",
			method:         http.MethodDelete,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice",
			wantPermission: catalog.PermWorkspaceEnvDelete,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			// Repairing may recreate a branch and a virtual environment, so it
			// carries the same weight as creating an environment.
			name:           "reconciliation needs the create permission, not read",
			method:         http.MethodPost,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/reconcile",
			wantPermission: catalog.PermWorkspaceEnvCreate,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "environment events fall back to the group read rule",
			method:         http.MethodGet,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/events",
			wantPermission: catalog.PermWorkspaceEnvRead,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "file write falls back to the group write rule",
			method:         http.MethodPost,
			path:           "/api/v1/files/write",
			wantPermission: catalog.PermWorkspaceCodeWrite,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "file read falls back to the group read rule",
			method:         http.MethodGet,
			path:           "/api/v1/files/list",
			wantPermission: catalog.PermWorkspaceCodeRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "apply requires the apply permission, not plan preview",
			method:         http.MethodPost,
			path:           "/api/v1/sqlmesh/apply",
			wantPermission: catalog.PermWorkspacePlanApply,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "git push is separated from generic code writes",
			method:         http.MethodPost,
			path:           "/api/v1/git/push",
			wantPermission: catalog.PermWorkspaceGitPush,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "git credentials are user scoped",
			method:         http.MethodPost,
			path:           "/api/v1/git/credentials",
			wantPermission: "",
			wantSource:     ScopeSourceNone,
		},
		{
			name:           "log reads need their own permission, not code read",
			method:         http.MethodGet,
			path:           "/api/v1/logs",
			wantPermission: catalog.PermWorkspaceLogsRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "the log tail is guarded like the log list",
			method:         http.MethodGet,
			path:           "/api/v1/logs/stream",
			wantPermission: catalog.PermWorkspaceLogsRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "the error archive is part of the log surface",
			method:         http.MethodGet,
			path:           "/api/v1/logs/errors/ab12cd34-2026-08-21T09:30:12",
			wantPermission: catalog.PermWorkspaceLogsRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rule := MatchRoute(tc.method, tc.path)
			if rule.Deny {
				t.Fatalf("rule unexpectedly denied")
			}
			if rule.Permission != tc.wantPermission {
				t.Errorf("permission = %q, want %q", rule.Permission, tc.wantPermission)
			}
			if rule.Source != tc.wantSource {
				t.Errorf("scope source = %v, want %v", rule.Source, tc.wantSource)
			}
		})
	}
}

// TestMatchRouteFallbackPolicy locks the default-deny behaviour for unmapped
// state-changing requests while keeping unmapped reads reachable.
func TestMatchRouteFallbackPolicy(t *testing.T) {
	if rule := MatchRoute(http.MethodPost, "/api/v1/unknown/thing"); !rule.Deny {
		t.Error("an unmapped write must be denied")
	}
	if rule := MatchRoute(http.MethodDelete, "/api/v1/unknown/thing"); !rule.Deny {
		t.Error("an unmapped delete must be denied")
	}
	// Environment actions not released yet (promote) must stay closed instead of
	// inheriting the environment read rule.
	if rule := MatchRoute(http.MethodPost,
		"/api/v1/environments/ws-t1-demo:dev_alice/promote"); !rule.Deny {
		t.Error("an unreleased environment action must be denied")
	}
	rule := MatchRoute(http.MethodGet, "/api/v1/unknown/thing")
	if rule.Deny || rule.Source != ScopeSourceNone || rule.Permission != "" {
		t.Errorf("an unmapped read must degrade to authenticated-only, got %+v", rule)
	}
}

// TestLogRoutesRequireResolvableWorkspace locks the one property that keeps a
// log query from leaking: it must not degrade to "has the permission in some
// workspace" when the path cannot be resolved.
func TestLogRoutesRequireResolvableWorkspace(t *testing.T) {
	for _, path := range []string{"/api/v1/logs", "/api/v1/logs/errors"} {
		rule := MatchRoute(http.MethodGet, path)
		if rule.ScopeOptional {
			t.Errorf("%s must require a resolvable workspace", path)
		}
	}
}

// TestQuotaRouteIsTenantScopedReadOnly pins the two properties that make the
// quota endpoint safe to expose without a permission (contract 08 §6).
//
// The endpoint reports the CALLER'S OWN tenant, resolved from the identity
// header, so there is no scope to authorize against — but that is only true
// while (a) the scope is the tenant rather than something a client could name,
// and (b) no write method inherits the rule. A future admin endpoint raising a
// limit must be declared deliberately, not arrive by accident.
func TestQuotaRouteIsTenantScopedReadOnly(t *testing.T) {
	rule := MatchRoute(http.MethodGet, "/api/v1/quotas")
	if rule.Deny {
		t.Fatal("reading one's own quota must be allowed")
	}
	if rule.Source != ScopeSourceTenant {
		t.Errorf("scope source = %v, want tenant", rule.Source)
	}
	if rule.Permission != "" {
		t.Errorf("permission = %q, want authenticated-only", rule.Permission)
	}

	for _, method := range []string{
		http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete,
	} {
		if r := MatchRoute(method, "/api/v1/quotas"); !r.Deny {
			t.Errorf("%s /api/v1/quotas = %+v, want denied", method, r)
		}
	}
}

// TestSegmentAt covers the workspace id extraction used before chi matches.
func TestSegmentAt(t *testing.T) {
	segment, ok := SegmentAt("/api/v1/workspaces/ws-t1-demo/members", 3)
	if !ok || segment != "ws-t1-demo" {
		t.Errorf("segment = %q ok = %v, want ws-t1-demo true", segment, ok)
	}
	if _, ok := SegmentAt("/api/v1/workspaces", 3); ok {
		t.Error("out-of-range segment must not resolve")
	}
}

// TestBelongsToTenant covers the cross-tenant guard encoded in the identifier.
func TestBelongsToTenant(t *testing.T) {
	if !BelongsToTenant("ws-t1-demo", "t1") {
		t.Error("workspace of the same tenant must be accepted")
	}
	if BelongsToTenant("ws-t2-demo", "t1") {
		t.Error("workspace of another tenant must be rejected")
	}
	// A tenant id that is a prefix of another one must not be confused.
	if BelongsToTenant("ws-t12-demo", "t1") {
		t.Error("prefix collision must be rejected")
	}
	if BelongsToTenant("", "t1") || BelongsToTenant("ws-t1-demo", "") {
		t.Error("empty inputs must be rejected")
	}
}

// TestWorkspaceIDFromPath covers deriving the workspace from a working copy.
// Working copies are environment worktrees:
// {base}/{tenant}/{workspace}/{user}/envs/{environment}.
func TestWorkspaceIDFromPath(t *testing.T) {
	cases := []struct {
		name     string
		path     string
		tenantID string
		want     string
		wantOK   bool
	}{
		{
			name:     "canonical layout",
			path:     "/data/workspaces/t1/ws-t1-demo/u1/envs/prod",
			tenantID: "t1",
			want:     "ws-t1-demo",
			wantOK:   true,
		},
		{
			name:     "windows separators are normalized",
			path:     `C:\data\workspaces\t1\ws-t1-demo\u1\envs\dev_alice`,
			tenantID: "t1",
			want:     "ws-t1-demo",
			wantOK:   true,
		},
		{
			name:     "tenant mismatch in the directory layout",
			path:     "/data/workspaces/t2/ws-t2-demo/u1/envs/prod",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "traversal cannot forge another tenant",
			path:     "/data/workspaces/t1/ws-t1-demo/../../t2/ws-t2-demo/u1/envs/prod",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "the user root is not a working copy",
			path:     "/data/workspaces/t1/ws-t1-demo/u1",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "the shared object database is not a working copy",
			path:     "/data/workspaces/t1/ws-t1-demo/u1/.repo",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "a nested directory inside a worktree is rejected",
			path:     "/data/workspaces/t1/ws-t1-demo/u1/envs/prod/sql-warehouse",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "too shallow",
			path:     "/t1/ws-t1-demo",
			tenantID: "t1",
			wantOK:   false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := WorkspaceIDFromPath(tc.path, tc.tenantID)
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if ok && got != tc.want {
				t.Errorf("workspace id = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestParseWorkingCopy covers the full addressing tuple a path carries, which
// is what makes every downstream service environment-aware without a new
// request parameter.
func TestParseWorkingCopy(t *testing.T) {
	working, ok := ParseWorkingCopy("/data/workspaces/t1/ws-t1-demo/u1/envs/dev_alice")
	if !ok {
		t.Fatal("the canonical layout must parse")
	}
	if working.TenantID != "t1" || working.WorkspaceID != "ws-t1-demo" ||
		working.UserID != "u1" || working.Environment != "dev_alice" {
		t.Errorf("unexpected tuple: %+v", working)
	}
}

// TestOwnsWorkingCopy guards the per-member isolation of working copies: they
// hold uncommitted work, so membership in the workspace is not enough.
func TestOwnsWorkingCopy(t *testing.T) {
	const copyPath = "/data/workspaces/t1/ws-t1-demo/u1/envs/prod"
	if !OwnsWorkingCopy(copyPath, "u1") {
		t.Error("the owning user must be accepted")
	}
	if OwnsWorkingCopy(copyPath, "u2") {
		t.Error("another member of the same workspace must be rejected")
	}
	// The environment name must never be mistaken for the owner.
	if OwnsWorkingCopy(copyPath, "prod") {
		t.Error("the environment segment must not authorize as a user")
	}
	if OwnsWorkingCopy(copyPath, "") {
		t.Error("an anonymous caller must be rejected")
	}
}

// TestWorkspaceIDFromEnvironmentID covers the "{workspace_id}:{environment}"
// identifier the registry API is addressed by.
func TestWorkspaceIDFromEnvironmentID(t *testing.T) {
	got, ok := WorkspaceIDFromEnvironmentID("ws-t1-demo:dev_alice", "t1")
	if !ok || got != "ws-t1-demo" {
		t.Errorf("workspace id = %q ok = %v, want ws-t1-demo true", got, ok)
	}
	if _, ok := WorkspaceIDFromEnvironmentID("ws-t2-demo:dev_alice", "t1"); ok {
		t.Error("another tenant's environment must be rejected")
	}
	if _, ok := WorkspaceIDFromEnvironmentID("ws-t1-demo", "t1"); ok {
		t.Error("an id without an environment part must be rejected")
	}
	if _, ok := WorkspaceIDFromEnvironmentID("ws-t1-demo:", "t1"); ok {
		t.Error("an empty environment part must be rejected")
	}
}

// TestRoutePermissionsAreRegistered is the check whose absence let a route be
// guarded by a permission that does not exist: "git:credential:write" was
// asserted by the control plane while being absent from the catalog, so the
// endpoint answered 403 to every role including admin, with no way to grant it.
//
// Catalog integrity itself (unique codes, templates within their scope, every
// permission reachable through some role) is asserted in the shared package,
// against the JSON declaration both languages are generated from.
func TestRoutePermissionsAreRegistered(t *testing.T) {
	for _, rule := range rules {
		if rule.Permission == "" {
			continue
		}
		if !catalog.IsCatalogPermission(rule.Permission) {
			t.Errorf("route %s %s requires %q, which is not a registered permission "+
				"point — no role can ever hold it",
				rule.Method, rule.Pattern, rule.Permission)
			continue
		}
		if len(catalog.TemplatesGranting(rule.Permission)) == 0 {
			t.Errorf("route %s %s requires %q, which no built-in role grants",
				rule.Method, rule.Pattern, rule.Permission)
		}
	}
}

// TestRoutePermissionScopeMatchesSource pins the other half of the mapping: a
// rule resolving a workspace must require a workspace-scoped permission, and a
// tenant rule a tenant-scoped one. A crossed pair is never a denial — it is a
// permission checked against a role that cannot carry it.
func TestRoutePermissionScopeMatchesSource(t *testing.T) {
	for _, rule := range rules {
		if rule.Permission == "" {
			continue
		}
		scope, ok := catalog.ScopeOf(rule.Permission)
		if !ok {
			continue // Already reported by TestRoutePermissionsAreRegistered.
		}
		want := catalog.ScopeWorkspace
		if rule.Source == ScopeSourceTenant {
			want = catalog.ScopeTenant
		}
		if scope != want {
			t.Errorf("route %s %s resolves a %s scope but requires %q, which is %s-scoped",
				rule.Method, rule.Pattern, want, rule.Permission, scope)
		}
	}
}

// TestGitCredentialRoutesStayUserScoped pins the authorization semantics of a
// personal access token: managing one's own credential is guarded by identity
// and ownership, not by a role permission. The control plane must agree — it
// once asserted an unregistered permission here, which is what made every
// credential write impossible.
func TestGitCredentialRoutesStayUserScoped(t *testing.T) {
	for _, tc := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/api/v1/git/credentials"},
		{http.MethodPost, "/api/v1/git/credentials"},
		{http.MethodPut, "/api/v1/git/credentials/cred-1"},
		{http.MethodPatch, "/api/v1/git/credentials/cred-1/default"},
		{http.MethodDelete, "/api/v1/git/credentials/cred-1"},
	} {
		rule := MatchRoute(tc.method, tc.path)
		if rule.Deny {
			t.Errorf("%s %s must be reachable", tc.method, tc.path)
		}
		if rule.Permission != "" || rule.Source != ScopeSourceNone {
			t.Errorf("%s %s = %+v, want an authenticated-only user-scoped rule",
				tc.method, tc.path, rule)
		}
	}
}
