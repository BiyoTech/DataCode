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
	rule := MatchRoute(http.MethodGet, "/api/v1/unknown/thing")
	if rule.Deny || rule.Source != ScopeSourceNone || rule.Permission != "" {
		t.Errorf("an unmapped read must degrade to authenticated-only, got %+v", rule)
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
			path:     "/data/workspaces/t1/ws-t1-demo/u1",
			tenantID: "t1",
			want:     "ws-t1-demo",
			wantOK:   true,
		},
		{
			name:     "windows separators are normalized",
			path:     `C:\data\workspaces\t1\ws-t1-demo\u1`,
			tenantID: "t1",
			want:     "ws-t1-demo",
			wantOK:   true,
		},
		{
			name:     "tenant mismatch in the directory layout",
			path:     "/data/workspaces/t2/ws-t2-demo/u1",
			tenantID: "t1",
			wantOK:   false,
		},
		{
			name:     "traversal cannot forge another tenant",
			path:     "/data/workspaces/t1/ws-t1-demo/../../t2/ws-t2-demo/u1",
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
