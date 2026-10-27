package authz

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/dbx"
	"github.com/datacode/identity-service/internal/migrate"
	"github.com/datacode/identity-service/internal/rbac"
)

func setup(t *testing.T) (*Service, *dbx.DB, *rbac.Service) {
	t.Helper()
	db, err := dbx.Open(dbx.SQLite, filepath.Join(t.TempDir(), "identity.db"))
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	ctx := context.Background()
	if _, err := migrate.Run(ctx, db, zap.NewNop()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	rbacService := rbac.NewService(rbac.NewStore(db), zap.NewNop())
	if err := rbacService.Bootstrap(ctx); err != nil {
		t.Fatalf("bootstrap rbac: %v", err)
	}
	return NewService(db, rbacService), db, rbacService
}

// seedMember makes a user a tenant member with the given role.
func seedMember(t *testing.T, db *dbx.DB, tenantID, userID, role string) {
	t.Helper()
	now := time.Now().UTC().Format(time.RFC3339)
	if _, err := db.ExecContext(context.Background(),
		`INSERT INTO tenant_members (id, tenant_id, user_id, role, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		userID+"-"+tenantID, tenantID, userID, role, now); err != nil {
		t.Fatalf("seed tenant member: %v", err)
	}
}

func TestResolveReturnsTenantRoleAndPermissions(t *testing.T) {
	service, db, _ := setup(t)
	ctx := context.Background()
	seedMember(t, db, "t1", "u1", rbac.RoleTenantAdmin)

	snapshot, err := service.Resolve(ctx, "u1", "t1")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if snapshot.TenantRole != rbac.RoleTenantAdmin {
		t.Errorf("tenant role = %q, want %q", snapshot.TenantRole, rbac.RoleTenantAdmin)
	}
	if len(snapshot.TenantPermissions) == 0 {
		t.Error("a tenant admin must resolve to a non-empty permission set")
	}
	if snapshot.PermsVersion < 1 {
		t.Errorf("perms version = %d, want >= 1", snapshot.PermsVersion)
	}
	if snapshot.ResolvedAt.IsZero() || snapshot.ResolvedAt.Location() != time.UTC {
		t.Errorf("resolved_at must be a UTC instant, got %v", snapshot.ResolvedAt)
	}
}

// A non-member must resolve to an EMPTY snapshot rather than an error: the
// gateway needs "no permissions" to deny, and an error would surface as a 500.
func TestResolveNonMemberYieldsEmptySnapshot(t *testing.T) {
	service, _, _ := setup(t)

	snapshot, err := service.Resolve(context.Background(), "stranger", "t1")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if snapshot.TenantRole != "" {
		t.Errorf("tenant role = %q, want empty", snapshot.TenantRole)
	}
	if len(snapshot.TenantPermissions) != 0 {
		t.Errorf("permissions = %v, want none", snapshot.TenantPermissions)
	}
}

func TestResolveRequiresBothIdentifiers(t *testing.T) {
	service, _, _ := setup(t)
	ctx := context.Background()

	if _, err := service.Resolve(ctx, "", "t1"); err == nil {
		t.Error("empty user_id must be rejected")
	}
	if _, err := service.Resolve(ctx, "u1", ""); err == nil {
		t.Error("empty tenant_id must be rejected")
	}
}

// The version is what lets the gateway cache a snapshot safely: a bump has to
// be observable, otherwise a revocation would only take effect after the TTL.
func TestBumpAdvancesPermsVersion(t *testing.T) {
	service, _, _ := setup(t)
	ctx := context.Background()

	before, err := service.PermsVersion(ctx, "t1")
	if err != nil {
		t.Fatalf("read version: %v", err)
	}
	if err := service.Bump(ctx, "t1"); err != nil {
		t.Fatalf("bump: %v", err)
	}
	after, err := service.PermsVersion(ctx, "t1")
	if err != nil {
		t.Fatalf("re-read version: %v", err)
	}
	if after <= before {
		t.Errorf("version did not advance: %d -> %d", before, after)
	}
}

// Bumping a tenant that has no version row yet must still produce a value a
// stale client disagrees with (it would otherwise implicitly hold "1").
func TestBumpSeedsAboveImplicitDefault(t *testing.T) {
	service, _, _ := setup(t)
	ctx := context.Background()

	if err := service.Bump(ctx, "fresh-tenant"); err != nil {
		t.Fatalf("bump: %v", err)
	}
	version, err := service.PermsVersion(ctx, "fresh-tenant")
	if err != nil {
		t.Fatalf("read version: %v", err)
	}
	if version < 2 {
		t.Errorf("version = %d, want >= 2 so an implicit 1 is stale", version)
	}
}

// Workspace bindings must come back keyed by workspace, with the permission
// set keyed by ROLE — that shape is what lets the gateway evaluate both
// "in this workspace" and "in any workspace" without another round trip.
func TestResolveIncludesWorkspaceBindings(t *testing.T) {
	service, db, rbacService := setup(t)
	ctx := context.Background()
	seedMember(t, db, "t1", "u1", rbac.RoleTenantDeveloper)

	if err := rbacService.GrantWorkspaceOwner(ctx, "t1", "ws-t1-alpha", "u1"); err != nil {
		t.Fatalf("grant workspace owner: %v", err)
	}

	snapshot, err := service.Resolve(ctx, "u1", "t1")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	role, bound := snapshot.WorkspaceRoles["ws-t1-alpha"]
	if !bound {
		t.Fatalf("workspace binding missing: %#v", snapshot.WorkspaceRoles)
	}
	if _, hasPerms := snapshot.WorkspacePermissions[role]; !hasPerms {
		t.Errorf("permissions for role %q missing: %#v", role, snapshot.WorkspacePermissions)
	}
}
