package rbac

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// sourceCatalog mirrors shared/rbac-catalog.json, which the Python tests read
// too. Permissions is a json.RawMessage because a role either enumerates codes
// or wildcards its whole scope.
type sourceCatalog struct {
	Scopes      []string `json:"scopes"`
	Permissions []struct {
		Code        string `json:"code"`
		Scope       string `json:"scope"`
		Resource    string `json:"resource"`
		Action      string `json:"action"`
		Description string `json:"description"`
	} `json:"permissions"`
	Roles []struct {
		Code        string          `json:"code"`
		Scope       string          `json:"scope"`
		Name        string          `json:"name"`
		Description string          `json:"description"`
		Permissions json.RawMessage `json:"permissions"`
	} `json:"roles"`
}

func loadSource(t *testing.T) sourceCatalog {
	t.Helper()
	// ../../rbac-catalog.json relative to shared/go/rbac.
	raw, err := os.ReadFile(filepath.Join("..", "..", "rbac-catalog.json"))
	if err != nil {
		t.Fatalf("cannot read the shared catalog: %v", err)
	}
	var src sourceCatalog
	if err := json.Unmarshal(raw, &src); err != nil {
		t.Fatalf("cannot decode the shared catalog: %v", err)
	}
	if len(src.Permissions) == 0 || len(src.Roles) == 0 {
		t.Fatal("the shared catalog is empty; this cross-language check is vacuous")
	}
	return src
}

// resolvePermissions expands the wildcard form the source uses for the owner
// role, so the expectation is computed from the SOURCE rather than from the
// generated table it is meant to verify.
func resolvePermissions(t *testing.T, src sourceCatalog, scope string, raw json.RawMessage) []string {
	t.Helper()
	var enumerated []string
	if err := json.Unmarshal(raw, &enumerated); err == nil {
		return enumerated
	}
	var wildcard string
	if err := json.Unmarshal(raw, &wildcard); err != nil {
		t.Fatalf("role permissions are neither a list nor a wildcard: %s", raw)
	}
	if wildcard != "*"+scope {
		t.Fatalf("wildcard %q does not match the role scope %q", wildcard, scope)
	}
	var codes []string
	for _, perm := range src.Permissions {
		if perm.Scope == scope {
			codes = append(codes, perm.Code)
		}
	}
	return codes
}

// TestCatalogMatchesSharedSource is the cross-language guarantee for the
// permission vocabulary: a code that exists on one side only would let a route
// (or a service-side assertion) require a permission that no role can hold —
// which is exactly the failure mode that shipped as a permanent 403.
func TestCatalogMatchesSharedSource(t *testing.T) {
	src := loadSource(t)

	if len(Catalog) != len(src.Permissions) {
		t.Fatalf("catalog has %d permissions, the source declares %d",
			len(Catalog), len(src.Permissions))
	}
	for i, want := range src.Permissions {
		got := Catalog[i]
		if got.Code != want.Code || string(got.Scope) != want.Scope ||
			got.Resource != want.Resource || got.Action != want.Action ||
			got.Description != want.Description {
			t.Errorf("permission %d: got %+v, source declares %+v", i, got, want)
		}
	}

	if len(Scopes) != len(src.Scopes) {
		t.Fatalf("Scopes has %d entries, the source declares %d", len(Scopes), len(src.Scopes))
	}
	for i, want := range src.Scopes {
		if string(Scopes[i]) != want {
			t.Errorf("scope %d = %q, source declares %q", i, Scopes[i], want)
		}
	}
}

// TestRoleTemplatesMatchSharedSource pins the built-in role composition. The
// seeder writes these rows, so a drift silently changes what a fresh tenant's
// admin can do.
func TestRoleTemplatesMatchSharedSource(t *testing.T) {
	src := loadSource(t)

	if len(BuiltinRoleTemplates) != len(src.Roles) {
		t.Fatalf("there are %d role templates, the source declares %d",
			len(BuiltinRoleTemplates), len(src.Roles))
	}
	for i, want := range src.Roles {
		got := BuiltinRoleTemplates[i]
		if got.Code != want.Code || string(got.Scope) != want.Scope ||
			got.Name != want.Name || got.Description != want.Description {
			t.Errorf("role %d: got %s/%s %q, source declares %s/%s %q",
				i, got.Scope, got.Code, got.Name, want.Scope, want.Code, want.Name)
		}
		expected := resolvePermissions(t, src, want.Scope, want.Permissions)
		if len(got.Permissions) != len(expected) {
			t.Errorf("role %s/%s grants %d permissions, source declares %d",
				got.Scope, got.Code, len(got.Permissions), len(expected))
			continue
		}
		for j, code := range expected {
			if got.Permissions[j] != code {
				t.Errorf("role %s/%s permission %d = %q, source declares %q",
					got.Scope, got.Code, j, got.Permissions[j], code)
			}
		}
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

// TestEveryPermissionIsReachable is the invariant this package exists to
// protect: a permission point nobody can be granted is a dead endpoint, not a
// stricter one. Every catalog code must be reachable through at least one
// built-in role template.
func TestEveryPermissionIsReachable(t *testing.T) {
	for _, def := range Catalog {
		if len(TemplatesGranting(def.Code)) == 0 {
			t.Errorf("permission %s is granted by no built-in role, so no account "+
				"can ever hold it", def.Code)
		}
	}
}

// TestPermissionsInScopeFollowsCatalog pins the owner template's expansion: the
// role must gain a newly declared workspace permission automatically.
func TestPermissionsInScopeFollowsCatalog(t *testing.T) {
	workspace := PermissionsInScope(ScopeWorkspace)
	if len(workspace) == 0 {
		t.Fatal("no workspace permissions resolved")
	}
	for _, code := range workspace {
		scope, ok := ScopeOf(code)
		if !ok || scope != ScopeWorkspace {
			t.Errorf("PermissionsInScope returned %s, which is not workspace-scoped", code)
		}
	}
	if len(PermissionsInScope("nonexistent")) != 0 {
		t.Error("an unknown scope must resolve to no permissions")
	}
}

// TestIsCatalogPermissionRejectsUnregistered is the check every permission
// assertion goes through. "git:credential:write" is the code that shipped as an
// unreachable assertion; it must stay rejected.
func TestIsCatalogPermissionRejectsUnregistered(t *testing.T) {
	for _, code := range []string{"", "git:credential:write", "workspace:manage", "workspace:read:extra"} {
		if IsCatalogPermission(code) {
			t.Errorf("%q must not be a catalog permission", code)
		}
	}
	if !IsCatalogPermission(PermWorkspaceRead) {
		t.Errorf("%q must be a catalog permission", PermWorkspaceRead)
	}
}
