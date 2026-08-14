package rbac

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/auth"
)

// newTestService builds an RBAC service on a throwaway SQLite database with the
// catalog synchronized and the template roles seeded.
func newTestService(t *testing.T) *Service {
	t.Helper()

	store, err := auth.NewStore(auth.DialectSQLite, filepath.Join(t.TempDir(), "rbac.db"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })

	service := NewService(NewStore(store.DB()), zap.NewNop())
	if err := service.Bootstrap(context.Background()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	return service
}

// TestBootstrapIsIdempotent checks that restarting the gateway neither
// duplicates catalog rows nor template roles.
func TestBootstrapIsIdempotent(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.Bootstrap(ctx); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	permissions, err := service.ListPermissions(ctx)
	if err != nil {
		t.Fatalf("ListPermissions: %v", err)
	}
	if len(permissions) != len(Catalog) {
		t.Errorf("catalog size = %d, want %d", len(permissions), len(Catalog))
	}
}

// TestEnsureTenantRolesInstantiatesBuiltins verifies that a tenant receives its
// own editable copy of every built-in role.
func TestEnsureTenantRolesInstantiatesBuiltins(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}
	roles, err := service.ListRoles(ctx, "t1", "")
	if err != nil {
		t.Fatalf("ListRoles: %v", err)
	}
	if len(roles) != len(BuiltinRoleTemplates) {
		t.Fatalf("role count = %d, want %d", len(roles), len(BuiltinRoleTemplates))
	}
	for _, role := range roles {
		if !role.IsBuiltin {
			t.Errorf("role %s should be marked built-in", role.Code)
		}
		if len(role.Permissions) == 0 {
			t.Errorf("role %s was instantiated without permissions", role.Code)
		}
	}

	granted, err := service.Can(ctx, "t1", ScopeTenant, RoleTenantAdmin, PermTenantMemberWrite)
	if err != nil {
		t.Fatalf("Can: %v", err)
	}
	if !granted {
		t.Error("admin must be able to write tenant members")
	}
	granted, err = service.Can(ctx, "t1", ScopeTenant, RoleTenantViewer, PermTenantMemberWrite)
	if err != nil {
		t.Fatalf("Can: %v", err)
	}
	if granted {
		t.Error("viewer must not be able to write tenant members")
	}
}

// TestTenantEditOfBuiltinRoleSurvivesReseed is the core consistency guarantee:
// the seeder owns the templates, the tenant owns its instances.
func TestTenantEditOfBuiltinRoleSurvivesReseed(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}
	viewer, err := service.RoleByCode(ctx, "t1", ScopeTenant, RoleTenantViewer)
	if err != nil {
		t.Fatalf("RoleByCode: %v", err)
	}

	// The tenant decides that its read-only role may also create workspaces.
	if _, err := service.UpdateRole(ctx, "actor", "t1", viewer.ID,
		"Auditor", "customized",
		[]string{PermTenantMemberRead, PermTenantWorkspaceCreate},
	); err != nil {
		t.Fatalf("UpdateRole: %v", err)
	}

	// A restart re-runs the seeding and the lazy instantiation.
	if err := service.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if _, err := service.store.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("store EnsureTenantRoles: %v", err)
	}
	service.InvalidateTenant("t1")

	granted, err := service.Can(ctx, "t1", ScopeTenant, RoleTenantViewer, PermTenantWorkspaceCreate)
	if err != nil {
		t.Fatalf("Can: %v", err)
	}
	if !granted {
		t.Error("the tenant customization of a built-in role must not be reverted")
	}

	reloaded, err := service.RoleByCode(ctx, "t1", ScopeTenant, RoleTenantViewer)
	if err != nil {
		t.Fatalf("RoleByCode: %v", err)
	}
	if reloaded.Name != "Auditor" {
		t.Errorf("role name = %q, want %q", reloaded.Name, "Auditor")
	}
	if !reloaded.IsBuiltin {
		t.Error("an edited built-in role stays built-in")
	}
}

// TestEnsureTenantRolesUpgradesStaleBuiltinPermissions reproduces the bug where
// a tenant's built-in role was instantiated before a permission was added to the
// template (e.g. tenant:workspace:join on developer). The lazy instantiation
// must backfill the new permission instead of leaving the role stale.
func TestEnsureTenantRolesUpgradesStaleBuiltinPermissions(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}
	developer, err := service.RoleByCode(ctx, "t1", ScopeTenant, RoleTenantDeveloper)
	if err != nil {
		t.Fatalf("RoleByCode: %v", err)
	}

	// Simulate a stale instance created before PermTenantWorkspaceJoin was
	// added to the template: strip the permission the template now ships.
	if err := service.store.ReplaceRolePermissions(ctx, developer.ID,
		filterOut(developer.Permissions, PermTenantWorkspaceJoin)); err != nil {
		t.Fatalf("strip permission: %v", err)
	}
	service.InvalidateTenant("t1")

	granted, err := service.Can(ctx, "t1", ScopeTenant, RoleTenantDeveloper, PermTenantWorkspaceJoin)
	if err != nil {
		t.Fatalf("Can before upgrade: %v", err)
	}
	if granted {
		t.Fatal("precondition: developer must lack workspace:join before upgrade")
	}

	// A gateway restart re-runs the lazy instantiation, which must detect the
	// stale un-customized role and backfill the missing permission.
	if _, err := service.store.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("upgrade EnsureTenantRoles: %v", err)
	}
	service.InvalidateTenant("t1")

	granted, err = service.Can(ctx, "t1", ScopeTenant, RoleTenantDeveloper, PermTenantWorkspaceJoin)
	if err != nil {
		t.Fatalf("Can after upgrade: %v", err)
	}
	if !granted {
		t.Error("developer must regain workspace:join after the lazy upgrade")
	}
}

// filterOut returns items without the given value.
func filterOut(items []string, drop string) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		if item != drop {
			out = append(out, item)
		}
	}
	return out
}

// TestCustomRoleLifecycle covers creation, validation and deletion rules.
func TestCustomRoleLifecycle(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}

	role, err := service.CreateRole(ctx, "actor", "t1", ScopeWorkspace,
		"data_steward", "Data Steward", "curates models",
		[]string{PermWorkspaceRead, PermWorkspaceCodeRead},
	)
	if err != nil {
		t.Fatalf("CreateRole: %v", err)
	}
	if role.IsBuiltin {
		t.Error("a tenant-created role must not be built-in")
	}

	// A permission from another scope must be rejected.
	if _, err := service.CreateRole(ctx, "actor", "t1", ScopeWorkspace,
		"broken", "Broken", "", []string{PermTenantMemberWrite},
	); err == nil {
		t.Error("cross-scope permission must be rejected")
	}
	// An unregistered permission code must be rejected.
	if _, err := service.CreateRole(ctx, "actor", "t1", ScopeWorkspace,
		"invented", "Invented", "", []string{"workspace:does:not:exist"},
	); err == nil {
		t.Error("unknown permission code must be rejected")
	}
	// The role code must be reusable as a stable identifier.
	if _, err := service.CreateRole(ctx, "actor", "t1", ScopeWorkspace,
		"Bad Code!", "Bad", "", nil,
	); err == nil {
		t.Error("invalid role code must be rejected")
	}
	// Duplicate codes must be rejected.
	if _, err := service.CreateRole(ctx, "actor", "t1", ScopeWorkspace,
		"data_steward", "Duplicate", "", nil,
	); err == nil {
		t.Error("duplicate role code must be rejected")
	}

	if err := service.DeleteRole(ctx, "actor", "t1", role.ID); err != nil {
		t.Fatalf("DeleteRole: %v", err)
	}

	// Built-in roles can never be deleted.
	builtin, err := service.RoleByCode(ctx, "t1", ScopeWorkspace, RoleWorkspaceEngineer)
	if err != nil {
		t.Fatalf("RoleByCode: %v", err)
	}
	if err := service.DeleteRole(ctx, "actor", "t1", builtin.ID); err == nil {
		t.Error("built-in role deletion must be rejected")
	}
}

// TestWorkspaceBindings covers grants, the protected owner binding and cascade
// removal when a tenant membership is revoked.
func TestWorkspaceBindings(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}
	const workspaceID = "ws-t1-demo"

	if err := service.GrantWorkspaceOwner(ctx, "t1", workspaceID, "owner-1"); err != nil {
		t.Fatalf("GrantWorkspaceOwner: %v", err)
	}
	if err := service.GrantWorkspaceMember(
		ctx, "owner-1", "t1", workspaceID, "member-1", RoleWorkspaceEngineer,
	); err != nil {
		t.Fatalf("GrantWorkspaceMember: %v", err)
	}

	// A workspace may have multiple owners: granting the owner role to a
	// second member is allowed.
	if err := service.GrantWorkspaceMember(
		ctx, "owner-1", "t1", workspaceID, "member-2", RoleWorkspaceOwner,
	); err != nil {
		t.Fatalf("granting a second owner must succeed: %v", err)
	}

	// With two owners, the first one may be downgraded to a non-owner role.
	if err := service.GrantWorkspaceMember(
		ctx, "member-2", "t1", workspaceID, "owner-1", RoleWorkspaceViewer,
	); err != nil {
		t.Fatalf("downgrading a non-last owner must succeed: %v", err)
	}

	// The last remaining owner (member-2) cannot be downgraded or revoked.
	if err := service.GrantWorkspaceMember(
		ctx, "member-2", "t1", workspaceID, "member-2", RoleWorkspaceViewer,
	); !errors.Is(err, ErrLastOwnerLocked) {
		t.Fatalf("downgrading the last owner must be rejected, got %v", err)
	}
	if err := service.RevokeWorkspaceMember(ctx, "member-2", "t1", workspaceID, "member-2"); !errors.Is(err, ErrLastOwnerLocked) {
		t.Fatalf("revoking the last owner must be rejected, got %v", err)
	}

	// An unknown role code must be rejected.
	if err := service.GrantWorkspaceMember(
		ctx, "member-2", "t1", workspaceID, "member-3", "ghost",
	); !errors.Is(err, ErrInvalidRoleCode) {
		t.Fatalf("unknown role code must be rejected, got %v", err)
	}

	granted, err := service.Can(ctx, "t1", ScopeWorkspace, RoleWorkspaceEngineer, PermWorkspaceGitPush)
	if err != nil {
		t.Fatalf("Can: %v", err)
	}
	if !granted {
		t.Error("engineer must be able to push")
	}
	granted, err = service.Can(ctx, "t1", ScopeWorkspace, RoleWorkspaceAnalyst, PermWorkspaceGitPush)
	if err != nil {
		t.Fatalf("Can: %v", err)
	}
	if granted {
		t.Error("analyst must not be able to push")
	}

	// Revoking the tenant membership must drop every workspace binding.
	if err := service.PurgeTenantMember(ctx, "admin-1", "t1", "member-1"); err != nil {
		t.Fatalf("PurgeTenantMember: %v", err)
	}
	role, err := service.WorkspaceRole(ctx, "t1", workspaceID, "member-1")
	if err != nil {
		t.Fatalf("WorkspaceRole: %v", err)
	}
	if role != "" {
		t.Errorf("binding survived the tenant membership revocation: %q", role)
	}
}

// TestCanInAnyWorkspaceRequiresARealGrant makes sure the fallback used by
// instance-wide endpoints is still tied to an existing binding.
func TestCanInAnyWorkspaceRequiresARealGrant(t *testing.T) {
	service := newTestService(t)
	ctx := context.Background()

	if err := service.EnsureTenantRoles(ctx, "t1"); err != nil {
		t.Fatalf("EnsureTenantRoles: %v", err)
	}

	granted, err := service.CanInAnyWorkspace(ctx, "t1", "nobody", PermWorkspacePipelineRun)
	if err != nil {
		t.Fatalf("CanInAnyWorkspace: %v", err)
	}
	if granted {
		t.Error("a user without any binding must not pass the fallback check")
	}

	if err := service.GrantWorkspaceOwner(ctx, "t1", "ws-t1-demo", "owner-1"); err != nil {
		t.Fatalf("GrantWorkspaceOwner: %v", err)
	}
	granted, err = service.CanInAnyWorkspace(ctx, "t1", "owner-1", PermWorkspacePipelineRun)
	if err != nil {
		t.Fatalf("CanInAnyWorkspace: %v", err)
	}
	if !granted {
		t.Error("an owner must pass the fallback check")
	}
}
