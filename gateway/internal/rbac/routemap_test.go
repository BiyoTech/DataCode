package rbac

import (
	"net/http"
	"testing"
)

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
			wantPermission: PermTenantMemberWrite,
			wantSource:     ScopeSourceTenant,
		},
		{
			// Repairing state_schema rewrites config.yaml: gated as a code
			// write and scoped by workspace_path in the body.
			name:           "state schema repair is a code write",
			method:         http.MethodPost,
			path:           "/api/v1/sqlmesh/config/state-schema/repair",
			wantPermission: PermWorkspaceCodeWrite,
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
			wantPermission: PermWorkspaceGitBind,
			wantSource:     ScopeSourceWorkspaceField,
		},
		{
			name:           "workspace member revocation",
			method:         http.MethodDelete,
			path:           "/api/v1/workspaces/ws-t1-demo/members/u2",
			wantPermission: PermWorkspaceMemberWrite,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "environment creation is separated from workspace writes",
			method:         http.MethodPost,
			path:           "/api/v1/workspaces/ws-t1-demo/environments",
			wantPermission: PermWorkspaceEnvCreate,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "environment list only needs the read permission",
			method:         http.MethodGet,
			path:           "/api/v1/workspaces/ws-t1-demo/environments",
			wantPermission: PermWorkspaceEnvRead,
			wantSource:     ScopeSourceWorkspaceSegment,
		},
		{
			name:           "opening a working copy is an ordinary workspace read",
			method:         http.MethodPost,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/open",
			wantPermission: PermWorkspaceRead,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "environment destruction needs the delete permission",
			method:         http.MethodDelete,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice",
			wantPermission: PermWorkspaceEnvDelete,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			// Repairing may recreate a branch and a virtual environment, so it
			// carries the same weight as creating an environment.
			name:           "reconciliation needs the create permission, not read",
			method:         http.MethodPost,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/reconcile",
			wantPermission: PermWorkspaceEnvCreate,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "environment events fall back to the group read rule",
			method:         http.MethodGet,
			path:           "/api/v1/environments/ws-t1-demo:dev_alice/events",
			wantPermission: PermWorkspaceEnvRead,
			wantSource:     ScopeSourceEnvironmentSegment,
		},
		{
			name:           "file write falls back to the group write rule",
			method:         http.MethodPost,
			path:           "/api/v1/files/write",
			wantPermission: PermWorkspaceCodeWrite,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "file read falls back to the group read rule",
			method:         http.MethodGet,
			path:           "/api/v1/files/list",
			wantPermission: PermWorkspaceCodeRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "apply requires the apply permission, not plan preview",
			method:         http.MethodPost,
			path:           "/api/v1/sqlmesh/apply",
			wantPermission: PermWorkspacePlanApply,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "git push is separated from generic code writes",
			method:         http.MethodPost,
			path:           "/api/v1/git/push",
			wantPermission: PermWorkspaceGitPush,
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
			wantPermission: PermWorkspaceLogsRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "the log tail is guarded like the log list",
			method:         http.MethodGet,
			path:           "/api/v1/logs/stream",
			wantPermission: PermWorkspaceLogsRead,
			wantSource:     ScopeSourceWorkspacePath,
		},
		{
			name:           "the error archive is part of the log surface",
			method:         http.MethodGet,
			path:           "/api/v1/logs/errors/ab12cd34-2026-08-21T09:30:12",
			wantPermission: PermWorkspaceLogsRead,
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

// TestCatalogIntegrity guards the invariants the seeder relies on: unique codes
// and role templates that only reference registered permissions of their scope.
func TestCatalogIntegrity(t *testing.T) {
	seen := make(map[string]struct{}, len(Catalog))
	for _, def := range Catalog {
		if _, duplicate := seen[def.Code]; duplicate {
			t.Errorf("duplicate permission code: %s", def.Code)
		}
		seen[def.Code] = struct{}{}
		if !IsValidScope(string(def.Scope)) {
			t.Errorf("permission %s has an unsupported scope %q", def.Code, def.Scope)
		}
	}

	for _, tpl := range BuiltinRoleTemplates {
		if len(tpl.Permissions) == 0 {
			t.Errorf("role template %s grants nothing", tpl.Code)
		}
		for _, code := range tpl.Permissions {
			scope, ok := ScopeOf(code)
			if !ok {
				t.Errorf("role template %s references unknown permission %s", tpl.Code, code)
				continue
			}
			if scope != tpl.Scope {
				t.Errorf("role template %s (%s) references %s from scope %s",
					tpl.Code, tpl.Scope, code, scope)
			}
		}
	}
}
