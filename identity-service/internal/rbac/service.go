package rbac

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"go.uber.org/zap"
)

// Validation errors surfaced by the role management use cases.
var (
	ErrInvalidRoleCode   = errors.New("invalid role code")
	ErrInvalidRoleName   = errors.New("invalid role name")
	ErrInvalidScope      = errors.New("invalid scope")
	ErrUnknownPermission = errors.New("unknown permission code")
	ErrScopeMismatch     = errors.New("permission does not belong to the role scope")
	ErrBuiltinImmutable  = errors.New("built-in role cannot be deleted")
	ErrRoleInUse         = errors.New("role is still assigned to members")
	ErrOwnerRoleLocked   = errors.New("workspace owner role cannot be assigned or revoked")
)

// maxRoleNameLength bounds the persisted role name column.
const maxRoleNameLength = 128

// Service exposes the RBAC use cases: catalog bootstrap, tenant role
// instantiation, permission decisions and role management.
type Service struct {
	store  *Store
	cache  *cache
	logger *zap.Logger

	// ensured memoizes tenants whose built-in roles were already instantiated,
	// so the lazy backfill costs at most one query per tenant per process.
	ensured sync.Map // map[string]struct{}
}

// NewService wires the RBAC service.
func NewService(store *Store, logger *zap.Logger) *Service {
	return &Service{
		store:  store,
		cache:  newCache(defaultCacheTTL),
		logger: logger,
	}
}

// Bootstrap synchronizes the permission catalog and the system template roles.
// It must succeed before the gateway starts serving traffic: without the
// catalog no authorization decision can be trusted.
func (s *Service) Bootstrap(parent context.Context) error {
	ctx, cancel := ctxWithTimeout(parent)
	defer cancel()

	inserted, deprecated, err := s.store.SyncPermissions(ctx)
	if err != nil {
		return err
	}
	if err := s.store.SeedTemplateRoles(ctx); err != nil {
		return err
	}
	s.logger.Info("rbac_bootstrap_completed",
		zap.Int("catalog_size", len(Catalog)),
		zap.Int("permissions_inserted", inserted),
		zap.Int("permissions_deprecated", deprecated),
		zap.Int("role_templates", len(BuiltinRoleTemplates)))
	return nil
}

// EnsureTenantRoles instantiates any built-in role the tenant does not own yet.
// It is safe to call on every tenant-scoped request: the result is memoized per
// process and never modifies existing tenant rows.
//
// Roles the tenant customized are left alone, so a permission newly added to a
// template does not reach them. That is reported as drift rather than being
// applied: overwriting the tenant's edit would silently revoke access, while
// staying silent would leave a permission point ungrantable for that tenant with
// nobody aware of it.
func (s *Service) EnsureTenantRoles(ctx context.Context, tenantID string) error {
	if tenantID == "" {
		return nil
	}
	if _, done := s.ensured.Load(tenantID); done {
		return nil
	}
	created, drift, err := s.store.EnsureTenantRoles(ctx, tenantID)
	if err != nil {
		return err
	}
	s.ensured.Store(tenantID, struct{}{})
	if created > 0 {
		s.cache.bump(tenantID)
		s.logger.Info("rbac_tenant_roles_instantiated",
			zap.String("tenant_id", tenantID), zap.Int("created", created))
	}
	for _, entry := range drift {
		s.logger.Warn("builtin_role_permission_drift",
			zap.String("tenant_id", entry.TenantID),
			zap.String("scope", string(entry.Scope)),
			zap.String("role_code", entry.RoleCode),
			zap.Strings("missing_permissions", entry.Missing))
	}
	return nil
}

// BuiltinRoleDrift reports the built-in roles of one tenant that lack a
// permission their current template grants. Read-only: the caller decides
// whether an administrator should grant it.
func (s *Service) BuiltinRoleDrift(ctx context.Context, tenantID string) ([]RoleDrift, error) {
	return s.store.ReportBuiltinRoleDrift(ctx, tenantID)
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

// permissionSet resolves the permission set of one role, using the cache.
func (s *Service) permissionSet(ctx context.Context, tenantID string, scope Scope, roleCode string) (map[string]struct{}, error) {
	if roleCode == "" {
		return nil, nil
	}
	if perms, ok := s.cache.getPermissions(tenantID, scope, roleCode); ok {
		return perms, nil
	}
	if err := s.EnsureTenantRoles(ctx, tenantID); err != nil {
		return nil, err
	}
	codes, err := s.store.PermissionsByRoleCode(ctx, tenantID, scope, roleCode)
	if err != nil {
		return nil, err
	}
	perms := make(map[string]struct{}, len(codes))
	for _, code := range codes {
		perms[code] = struct{}{}
	}
	s.cache.setPermissions(tenantID, scope, roleCode, perms)
	return perms, nil
}

// Can reports whether a role inside a scope grants a permission.
func (s *Service) Can(ctx context.Context, tenantID string, scope Scope, roleCode, permission string) (bool, error) {
	perms, err := s.permissionSet(ctx, tenantID, scope, roleCode)
	if err != nil {
		return false, err
	}
	_, granted := perms[permission]
	return granted, nil
}

// EffectivePermissions returns the sorted permission codes of a role, used to
// publish capabilities to downstream services and to the frontend.
func (s *Service) EffectivePermissions(ctx context.Context, tenantID string, scope Scope, roleCode string) ([]string, error) {
	perms, err := s.permissionSet(ctx, tenantID, scope, roleCode)
	if err != nil {
		return nil, err
	}
	codes := make([]string, 0, len(perms))
	for code := range perms {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	return codes, nil
}

// WorkspaceRole resolves the role a user holds in a workspace. An empty string
// with a nil error means "no binding", i.e. no access.
func (s *Service) WorkspaceRole(ctx context.Context, tenantID, workspaceID, userID string) (string, error) {
	if tenantID == "" || workspaceID == "" || userID == "" {
		return "", nil
	}
	if role, found, ok := s.cache.getWorkspaceRole(tenantID, workspaceID, userID); ok {
		if !found {
			return "", nil
		}
		return role, nil
	}
	role, err := s.store.WorkspaceRole(ctx, tenantID, workspaceID, userID)
	if errors.Is(err, ErrBindingNotFound) {
		s.cache.setWorkspaceRole(tenantID, workspaceID, userID, "", false)
		return "", nil
	}
	if err != nil {
		return "", err
	}
	s.cache.setWorkspaceRole(tenantID, workspaceID, userID, role, true)
	return role, nil
}

// ListWorkspaceRolesForUser returns workspace_id -> role_code for a user.
func (s *Service) ListWorkspaceRolesForUser(ctx context.Context, tenantID, userID string) (map[string]string, error) {
	return s.store.ListWorkspaceRolesForUser(ctx, tenantID, userID)
}

// CanInAnyWorkspace reports whether the user holds a permission in at least one
// workspace of the tenant. It backs instance-wide endpoints that carry no
// workspace reference (e.g. cancelling a run addressed only by run id): access
// is still tied to a real grant instead of being waved through.
func (s *Service) CanInAnyWorkspace(ctx context.Context, tenantID, userID, permission string) (bool, error) {
	bindings, err := s.store.ListWorkspaceRolesForUser(ctx, tenantID, userID)
	if err != nil {
		return false, err
	}
	checked := make(map[string]struct{}, len(bindings))
	for _, roleCode := range bindings {
		if _, done := checked[roleCode]; done {
			continue
		}
		checked[roleCode] = struct{}{}
		granted, err := s.Can(ctx, tenantID, ScopeWorkspace, roleCode, permission)
		if err != nil {
			return false, err
		}
		if granted {
			return true, nil
		}
	}
	return false, nil
}

// ---------------------------------------------------------------------------
// Role management
// ---------------------------------------------------------------------------

// ListPermissions returns the active permission catalog.
func (s *Service) ListPermissions(ctx context.Context) ([]PermissionDef, error) {
	return s.store.ListPermissions(ctx)
}

// ListRoles returns the tenant's roles, optionally filtered by scope.
func (s *Service) ListRoles(ctx context.Context, tenantID string, scope Scope) ([]Role, error) {
	if err := s.EnsureTenantRoles(ctx, tenantID); err != nil {
		return nil, err
	}
	return s.store.ListRoles(ctx, tenantID, scope)
}

// RoleByCode loads one role definition, used to render its display name.
func (s *Service) RoleByCode(ctx context.Context, tenantID string, scope Scope, code string) (*Role, error) {
	if err := s.EnsureTenantRoles(ctx, tenantID); err != nil {
		return nil, err
	}
	return s.store.GetRoleByCode(ctx, tenantID, scope, code)
}

// AssignableRoles returns the roles that may be bound to a member in a scope.
// The workspace owner role is assignable: a workspace may have multiple owners,
// and any existing owner may grant it to another tenant member.
func (s *Service) AssignableRoles(ctx context.Context, tenantID string, scope Scope) ([]Role, error) {
	return s.ListRoles(ctx, tenantID, scope)
}

// ValidateAssignable checks that a role code can be bound to a member in the
// given scope, rejecting unknown codes. The workspace owner role is allowed:
// granting it to additional members turns them into co-owners of the workspace.
func (s *Service) ValidateAssignable(ctx context.Context, tenantID string, scope Scope, roleCode string) error {
	if err := s.EnsureTenantRoles(ctx, tenantID); err != nil {
		return err
	}
	if _, err := s.store.GetRoleByCode(ctx, tenantID, scope, roleCode); err != nil {
		if errors.Is(err, ErrRoleNotFound) {
			return ErrInvalidRoleCode
		}
		return err
	}
	return nil
}

// CreateRole adds a tenant-owned custom role.
func (s *Service) CreateRole(
	ctx context.Context, actorID, tenantID string, scope Scope, code, name, description string, permissions []string,
) (*Role, error) {
	if err := s.EnsureTenantRoles(ctx, tenantID); err != nil {
		return nil, err
	}
	if !IsValidScope(string(scope)) {
		return nil, ErrInvalidScope
	}
	code = strings.ToLower(strings.TrimSpace(code))
	if err := validateRoleCode(code); err != nil {
		return nil, err
	}
	name = strings.TrimSpace(name)
	if name == "" || len(name) > maxRoleNameLength {
		return nil, ErrInvalidRoleName
	}
	if err := validatePermissions(scope, permissions); err != nil {
		return nil, err
	}

	role, err := s.store.CreateRole(ctx, tenantID, scope, code, name, description, permissions)
	if err != nil {
		return nil, err
	}
	s.cache.bump(tenantID)
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "role_created",
		ScopeType: string(scope), TargetID: role.ID,
		Detail: fmt.Sprintf("code=%s permissions=%d", code, len(role.Permissions)),
	})
	s.logger.Info("rbac_role_created",
		zap.String("tenant_id", tenantID), zap.String("actor_id", actorID),
		zap.String("scope", string(scope)), zap.String("role_code", code))
	return role, nil
}

// UpdateRole renames a role and replaces its permission composition. Built-in
// roles are editable this way; only their code stays immutable.
func (s *Service) UpdateRole(
	ctx context.Context, actorID, tenantID, roleID, name, description string, permissions []string,
) (*Role, error) {
	existing, err := s.store.GetRoleByID(ctx, tenantID, roleID)
	if err != nil {
		return nil, err
	}
	name = strings.TrimSpace(name)
	if name == "" || len(name) > maxRoleNameLength {
		return nil, ErrInvalidRoleName
	}
	if err := validatePermissions(existing.Scope, permissions); err != nil {
		return nil, err
	}
	if err := s.store.UpdateRole(ctx, tenantID, roleID, name, description, permissions); err != nil {
		return nil, err
	}
	s.cache.bump(tenantID)
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "role_updated",
		ScopeType: string(existing.Scope), TargetID: roleID,
		Detail: fmt.Sprintf("code=%s permissions=%d", existing.Code, len(dedupe(permissions))),
	})
	s.logger.Info("rbac_role_updated",
		zap.String("tenant_id", tenantID), zap.String("actor_id", actorID),
		zap.String("role_code", existing.Code), zap.Bool("builtin", existing.IsBuiltin))
	return s.store.GetRoleByID(ctx, tenantID, roleID)
}

// DeleteRole removes a custom role that no member is bound to.
func (s *Service) DeleteRole(ctx context.Context, actorID, tenantID, roleID string) error {
	existing, err := s.store.GetRoleByID(ctx, tenantID, roleID)
	if err != nil {
		return err
	}
	if existing.IsBuiltin {
		return ErrBuiltinImmutable
	}
	if existing.MemberCount > 0 {
		return ErrRoleInUse
	}
	if err := s.store.DeleteRole(ctx, tenantID, roleID); err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "role_deleted",
		ScopeType: string(existing.Scope), TargetID: roleID,
		Detail: "code=" + existing.Code,
	})
	s.logger.Info("rbac_role_deleted",
		zap.String("tenant_id", tenantID), zap.String("actor_id", actorID),
		zap.String("role_code", existing.Code))
	return nil
}

// ---------------------------------------------------------------------------
// Workspace bindings
// ---------------------------------------------------------------------------

// GrantWorkspaceOwner records the implicit owner binding created together with
// a workspace. It bypasses ValidateAssignable on purpose.
func (s *Service) GrantWorkspaceOwner(ctx context.Context, tenantID, workspaceID, userID string) error {
	if err := s.store.UpsertWorkspaceMember(
		ctx, tenantID, workspaceID, userID, RoleWorkspaceOwner, userID,
	); err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.logger.Info("rbac_workspace_owner_granted",
		zap.String("tenant_id", tenantID), zap.String("workspace_id", workspaceID),
		zap.String("user_id", userID))
	return nil
}

// ErrLastOwnerLocked protects a workspace from losing its last owner, which
// would leave it administratively orphaned.
var ErrLastOwnerLocked = errors.New("the last owner of a workspace cannot be removed or downgraded")

// GrantWorkspaceMember binds a tenant member to a workspace role.
//
// The workspace owner role is assignable: a workspace may have multiple owners,
// and any existing owner may grant it to another tenant member. Downgrading an
// existing owner to a different role is allowed only when at least one other
// owner remains, so the workspace is never left without administration.
func (s *Service) GrantWorkspaceMember(
	ctx context.Context, actorID, tenantID, workspaceID, userID, roleCode string,
) error {
	if err := s.ValidateAssignable(ctx, tenantID, ScopeWorkspace, roleCode); err != nil {
		return err
	}
	current, err := s.WorkspaceRole(ctx, tenantID, workspaceID, userID)
	if err != nil {
		return err
	}
	// Downgrading an existing owner to a non-owner role is a transfer of
	// ownership: block it when this member is the only owner left.
	if current == RoleWorkspaceOwner && roleCode != RoleWorkspaceOwner {
		count, err := s.store.CountWorkspaceOwners(ctx, tenantID, workspaceID)
		if err != nil {
			return err
		}
		if count <= 1 {
			return ErrLastOwnerLocked
		}
	}
	if err := s.store.UpsertWorkspaceMember(
		ctx, tenantID, workspaceID, userID, roleCode, actorID,
	); err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "workspace_member_granted",
		ScopeType: string(ScopeWorkspace), ScopeID: workspaceID,
		TargetID: userID, Detail: "role=" + roleCode,
	})
	s.logger.Info("rbac_workspace_member_granted",
		zap.String("tenant_id", tenantID), zap.String("workspace_id", workspaceID),
		zap.String("user_id", userID), zap.String("role_code", roleCode))
	return nil
}

// RevokeWorkspaceMember drops a workspace binding. An owner may be revoked only
// when another owner remains, so the workspace is never left without
// administration.
func (s *Service) RevokeWorkspaceMember(ctx context.Context, actorID, tenantID, workspaceID, userID string) error {
	current, err := s.WorkspaceRole(ctx, tenantID, workspaceID, userID)
	if err != nil {
		return err
	}
	if current == "" {
		return ErrBindingNotFound
	}
	if current == RoleWorkspaceOwner {
		count, err := s.store.CountWorkspaceOwners(ctx, tenantID, workspaceID)
		if err != nil {
			return err
		}
		if count <= 1 {
			return ErrLastOwnerLocked
		}
	}
	if err := s.store.RemoveWorkspaceMember(ctx, tenantID, workspaceID, userID); err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "workspace_member_revoked",
		ScopeType: string(ScopeWorkspace), ScopeID: workspaceID, TargetID: userID,
	})
	s.logger.Info("rbac_workspace_member_revoked",
		zap.String("tenant_id", tenantID), zap.String("workspace_id", workspaceID),
		zap.String("user_id", userID))
	return nil
}

// ListWorkspaceMembers returns the bindings of one workspace.
func (s *Service) ListWorkspaceMembers(ctx context.Context, tenantID, workspaceID string) ([]WorkspaceMember, error) {
	return s.store.ListWorkspaceMembers(ctx, tenantID, workspaceID)
}

// PurgeWorkspace removes every binding of a deleted workspace.
func (s *Service) PurgeWorkspace(ctx context.Context, actorID, tenantID, workspaceID string) error {
	removed, err := s.store.RemoveWorkspaceBindings(ctx, tenantID, workspaceID)
	if err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.logger.Info("rbac_workspace_bindings_purged",
		zap.String("tenant_id", tenantID), zap.String("workspace_id", workspaceID),
		zap.Int64("removed", removed))
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "workspace_purged",
		ScopeType: string(ScopeWorkspace), ScopeID: workspaceID,
		Detail: fmt.Sprintf("removed=%d", removed),
	})
	return nil
}

// PurgeTenantMember cascades a tenant membership revocation to every workspace
// binding the user held inside that tenant.
func (s *Service) PurgeTenantMember(ctx context.Context, actorID, tenantID, userID string) error {
	removed, err := s.store.RemoveTenantUserBindings(ctx, tenantID, userID)
	if err != nil {
		return err
	}
	s.cache.bump(tenantID)
	s.logger.Info("rbac_tenant_member_bindings_purged",
		zap.String("tenant_id", tenantID), zap.String("user_id", userID),
		zap.Int64("removed", removed))
	s.audit(ctx, AuditEntry{
		TenantID: tenantID, ActorID: actorID, Action: "tenant_member_purged",
		ScopeType: string(ScopeTenant), TargetID: userID,
		Detail: fmt.Sprintf("removed=%d", removed),
	})
	return nil
}

// InvalidateTenant drops cached decisions of a tenant, used after a tenant
// membership role change performed by the auth domain.
func (s *Service) InvalidateTenant(tenantID string) {
	s.cache.bump(tenantID)
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

// audit persists an audit record; a failure never breaks the request.
func (s *Service) audit(ctx context.Context, entry AuditEntry) {
	if err := s.store.WriteAudit(ctx, entry); err != nil {
		s.logger.Warn("rbac_audit_write_failed",
			zap.String("tenant_id", entry.TenantID),
			zap.String("action", entry.Action), zap.Error(err))
	}
}

// validateRoleCode enforces a stable, URL-safe role identifier.
func validateRoleCode(code string) error {
	if len(code) < 2 || len(code) > 64 {
		return ErrInvalidRoleCode
	}
	for _, r := range code {
		isAllowed := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' || r == '-'
		if !isAllowed {
			return ErrInvalidRoleCode
		}
	}
	return nil
}

// validatePermissions rejects codes outside the catalog and codes belonging to
// a different scope than the role.
func validatePermissions(scope Scope, permissions []string) error {
	for _, code := range permissions {
		permScope, ok := ScopeOf(code)
		if !ok {
			return fmt.Errorf("%w: %s", ErrUnknownPermission, code)
		}
		if permScope != scope {
			return fmt.Errorf("%w: %s", ErrScopeMismatch, code)
		}
	}
	return nil
}
