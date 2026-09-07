// Package authz serves the authorization SNAPSHOT the gateway decides with
// (design 02 §2.2, design 08 §1).
//
// Why a snapshot instead of a "can this user do X" RPC: the gateway must
// authorize EVERY request, so a per-check round trip would put identity on the
// hot path of the whole platform. One snapshot per (user, tenant) — cached for
// a short TTL and invalidated by `perms_version` — lets the gateway answer
// locally, including the "in any workspace" case that used to need a separate
// query.
//
// The snapshot is deliberately complete for that purpose and nothing more: it
// carries the tenant role's permissions, the caller's workspace bindings, and
// the permission set of each role those bindings reference. Everything the
// former in-gateway `decide()` needed, and no user-identifying data beyond it.
package authz

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/datacode/identity-service/internal/dbx"
	"github.com/datacode/identity-service/internal/rbac"
)

// Snapshot is the authorization state of one (user, tenant) pair.
type Snapshot struct {
	UserID   string `json:"user_id"`
	TenantID string `json:"tenant_id"`
	// TenantRole is the caller's role inside the tenant ("" when not a member).
	TenantRole string `json:"tenant_role"`
	// TenantPermissions is the effective permission set of TenantRole.
	TenantPermissions []string `json:"tenant_permissions"`
	// WorkspaceRoles maps workspace_id -> role_code for this caller.
	WorkspaceRoles map[string]string `json:"workspace_roles"`
	// WorkspacePermissions maps role_code -> effective permissions, for the
	// roles referenced by WorkspaceRoles. Keyed by ROLE, not by workspace, so
	// a caller with fifty workspaces on the same role transfers one set.
	WorkspacePermissions map[string][]string `json:"workspace_permissions"`
	// PermsVersion is the tenant's authorization generation. The gateway
	// carries it in the JWT and drops its cache when the two disagree.
	PermsVersion int64 `json:"perms_version"`
	// ResolvedAt is when this snapshot was computed (UTC).
	ResolvedAt time.Time `json:"resolved_at"`
}

// Service computes snapshots and maintains the version counter.
type Service struct {
	db   *dbx.DB
	rbac *rbac.Service
}

func NewService(db *dbx.DB, rbacService *rbac.Service) *Service {
	return &Service{db: db, rbac: rbacService}
}

// Resolve builds the snapshot for one (user, tenant).
func (s *Service) Resolve(ctx context.Context, userID, tenantID string) (*Snapshot, error) {
	if userID == "" || tenantID == "" {
		return nil, fmt.Errorf("user_id and tenant_id are required")
	}

	// Built-in roles are instantiated lazily per tenant so existing tenants
	// pick up newly released roles without a migration. Previously done by
	// the gateway middleware on every request; doing it here keeps that
	// behaviour while removing it from the hot path.
	if err := s.rbac.EnsureTenantRoles(ctx, tenantID); err != nil {
		return nil, fmt.Errorf("ensure tenant roles: %w", err)
	}

	tenantRole, err := s.tenantRole(ctx, userID, tenantID)
	if err != nil {
		return nil, err
	}

	snapshot := &Snapshot{
		UserID:               userID,
		TenantID:             tenantID,
		TenantRole:           tenantRole,
		WorkspaceRoles:       map[string]string{},
		WorkspacePermissions: map[string][]string{},
		ResolvedAt:           time.Now().UTC(),
	}

	if tenantRole != "" {
		perms, permErr := s.rbac.EffectivePermissions(ctx, tenantID, rbac.ScopeTenant, tenantRole)
		if permErr != nil {
			return nil, fmt.Errorf("tenant permissions: %w", permErr)
		}
		snapshot.TenantPermissions = perms
	}

	wsRoles, err := s.rbac.ListWorkspaceRolesForUser(ctx, tenantID, userID)
	if err != nil {
		return nil, fmt.Errorf("workspace roles: %w", err)
	}
	for workspaceID, roleCode := range wsRoles {
		snapshot.WorkspaceRoles[workspaceID] = roleCode
		if _, known := snapshot.WorkspacePermissions[roleCode]; known {
			continue
		}
		perms, permErr := s.rbac.EffectivePermissions(ctx, tenantID, rbac.ScopeWorkspace, roleCode)
		if permErr != nil {
			return nil, fmt.Errorf("workspace permissions for role %q: %w", roleCode, permErr)
		}
		snapshot.WorkspacePermissions[roleCode] = perms
	}

	version, err := s.PermsVersion(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	snapshot.PermsVersion = version
	return snapshot, nil
}

// PermsVersion returns a tenant's authorization generation, creating the row
// on first read so callers never special-case "no version yet".
func (s *Service) PermsVersion(ctx context.Context, tenantID string) (int64, error) {
	row := s.db.QueryRowContext(ctx, 
		`SELECT version FROM tenant_perms_version WHERE tenant_id = ?`, tenantID)

	var version int64
	err := row.Scan(&version)
	if errors.Is(err, sql.ErrNoRows) {
		now := time.Now().UTC().Format(time.RFC3339)
		if _, insErr := s.db.ExecContext(ctx, 
			`INSERT INTO tenant_perms_version (tenant_id, version, updated_at) VALUES (?, 1, ?)`,
			tenantID, now); insErr != nil && !dbx.IsUniqueViolation(insErr) {
			return 0, fmt.Errorf("seed perms version: %w", insErr)
		}
		return 1, nil
	}
	if err != nil {
		return 0, fmt.Errorf("read perms version: %w", err)
	}
	return version, nil
}

// Bump advances a tenant's authorization generation.
//
// Called after any change to roles, role permissions or workspace bindings.
// It is what makes the gateway's cache safe to hold for a TTL: a permission
// revocation is visible on the caller's next request instead of after the TTL,
// because the version in their token no longer matches.
func (s *Service) Bump(ctx context.Context, tenantID string) error {
	now := time.Now().UTC().Format(time.RFC3339)
	res, err := s.db.ExecContext(ctx, 
		`UPDATE tenant_perms_version SET version = version + 1, updated_at = ? WHERE tenant_id = ?`,
		now, tenantID)
	if err != nil {
		return fmt.Errorf("bump perms version: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		// No row yet: seed at 2 so a client holding the implicit "1" also
		// sees a mismatch and refetches.
		if _, insErr := s.db.ExecContext(ctx, 
			`INSERT INTO tenant_perms_version (tenant_id, version, updated_at) VALUES (?, 2, ?)`,
			tenantID, now); insErr != nil && !dbx.IsUniqueViolation(insErr) {
			return fmt.Errorf("seed bumped perms version: %w", insErr)
		}
	}
	return nil
}

// tenantRole reads the caller's role in a tenant ("" when not a member).
func (s *Service) tenantRole(ctx context.Context, userID, tenantID string) (string, error) {
	row := s.db.QueryRowContext(ctx, 
		`SELECT role FROM tenant_members WHERE tenant_id = ? AND user_id = ?`, tenantID, userID)

	var role string
	err := row.Scan(&role)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read tenant membership: %w", err)
	}
	return role, nil
}
