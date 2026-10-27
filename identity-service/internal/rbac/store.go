package rbac

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/datacode/identity-service/internal/dbx"
)

// Sentinel errors mapped to HTTP codes by the handler layer.
var (
	ErrRoleNotFound    = errors.New("role not found")
	ErrRoleExists      = errors.New("role already exists")
	ErrBindingNotFound = errors.New("role binding not found")
)

// templateTenantID marks system template roles, which only the seeder writes.
const templateTenantID = ""

// Role is a role definition together with its permission composition.
type Role struct {
	ID          string   `json:"id"`
	TenantID    string   `json:"-"`
	Scope       Scope    `json:"scope"`
	Code        string   `json:"code"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	IsBuiltin   bool     `json:"isBuiltin"`
	Permissions []string `json:"permissions"`
	MemberCount int      `json:"memberCount"`
}

// WorkspaceMember is a workspace-scoped role binding.
type WorkspaceMember struct {
	TenantID    string `json:"-"`
	WorkspaceID string `json:"workspaceId"`
	UserID      string `json:"userId"`
	RoleCode    string `json:"role"`
	GrantedBy   string `json:"grantedBy"`
	// CreatedAt is a UTC RFC3339 string; the frontend renders it in local time.
	CreatedAt string `json:"createdAt"`
}

// AuditEntry records a single authorization-relevant change.
type AuditEntry struct {
	TenantID  string
	ActorID   string
	Action    string
	ScopeType string
	ScopeID   string
	TargetID  string
	Detail    string
}

// Store is the RBAC persistence layer. It reuses the auth database handle so
// role bindings and identities stay in the same transactional domain.
// All SQL uses bound parameters and all timestamps are stored in UTC.
type Store struct {
	db *dbx.DB
}

// NewStore wires the RBAC persistence layer on top of an existing handle.
//
// The handle carries its own dialect, so RBAC no longer has to guess the
// backend: the previous PRAGMA feature probe could only tell SQLite from
// "something else" and would have silently classified PostgreSQL as MySQL.
func NewStore(db *dbx.DB) *Store {
	return &Store{db: db}
}

// ---------------------------------------------------------------------------
// Permission catalog
// ---------------------------------------------------------------------------

// SyncPermissions reconciles the database catalog with the code-side catalog:
// missing codes are inserted, existing ones refreshed, and codes that no longer
// exist in code are flagged as deprecated (never deleted, so historical role
// compositions stay readable).
func (s *Store) SyncPermissions(ctx context.Context) (inserted, deprecated int, err error) {
	now := nowUTC()
	for _, def := range Catalog {
		res, updErr := s.db.ExecContext(ctx,
			`UPDATE permissions
			    SET scope = ?, resource = ?, action = ?, description = ?,
			        deprecated = '0', updated_at = ?
			  WHERE code = ?`,
			string(def.Scope), def.Resource, def.Action, def.Description, now, def.Code)
		if updErr != nil {
			return inserted, deprecated, fmt.Errorf("refresh permission %s: %w", def.Code, updErr)
		}
		if affected, _ := res.RowsAffected(); affected > 0 {
			continue
		}
		if _, insErr := s.db.ExecContext(ctx,
			`INSERT INTO permissions
			     (code, scope, resource, action, description, deprecated, created_at, updated_at)
			 VALUES (?, ?, ?, ?, ?, '0', ?, ?)`,
			def.Code, string(def.Scope), def.Resource, def.Action, def.Description, now, now,
		); insErr != nil {
			if isUniqueViolation(insErr) {
				// Concurrent startup of a second replica: the row exists now.
				continue
			}
			return inserted, deprecated, fmt.Errorf("insert permission %s: %w", def.Code, insErr)
		}
		inserted++
	}

	codes := make([]interface{}, 0, len(Catalog))
	for _, def := range Catalog {
		codes = append(codes, def.Code)
	}
	res, err := s.db.ExecContext(ctx,
		fmt.Sprintf(`UPDATE permissions SET deprecated = '1', updated_at = ?
		              WHERE deprecated = '0' AND code NOT IN (%s)`, placeholders(len(codes))),
		append([]interface{}{now}, codes...)...)
	if err != nil {
		return inserted, deprecated, fmt.Errorf("deprecate stale permissions: %w", err)
	}
	n, _ := res.RowsAffected()
	return inserted, int(n), nil
}

// ListPermissions returns the active permission catalog from the database.
func (s *Store) ListPermissions(ctx context.Context) ([]PermissionDef, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT code, scope, resource, action, description
		   FROM permissions WHERE deprecated = '0'
		  ORDER BY scope, resource, action`)
	if err != nil {
		return nil, fmt.Errorf("query permissions: %w", err)
	}
	defer rows.Close()

	var defs []PermissionDef
	for rows.Next() {
		var (
			def   PermissionDef
			scope string
		)
		if err := rows.Scan(&def.Code, &scope, &def.Resource, &def.Action, &def.Description); err != nil {
			return nil, fmt.Errorf("scan permission: %w", err)
		}
		def.Scope = Scope(scope)
		defs = append(defs, def)
	}
	return defs, rows.Err()
}

// ---------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------

// SeedTemplateRoles rewrites the system template roles (tenant_id = ”). Only
// templates are touched: tenant-owned rows are never modified here, which keeps
// tenant customizations of built-in roles intact across restarts.
func (s *Store) SeedTemplateRoles(ctx context.Context) error {
	for _, tpl := range BuiltinRoleTemplates {
		role, err := s.getRole(ctx, templateTenantID, tpl.Scope, tpl.Code)
		switch {
		case errors.Is(err, ErrRoleNotFound):
			role = &Role{ID: newID()}
			now := nowUTC()
			if _, err := s.db.ExecContext(ctx,
				`INSERT INTO roles
				     (id, tenant_id, scope, code, name, description, is_builtin, created_at, updated_at)
				 VALUES (?, ?, ?, ?, ?, ?, '1', ?, ?)`,
				role.ID, templateTenantID, string(tpl.Scope), tpl.Code,
				tpl.Name, tpl.Description, now, now,
			); err != nil {
				if isUniqueViolation(err) {
					continue // Created concurrently by another replica.
				}
				return fmt.Errorf("insert template role %s: %w", tpl.Code, err)
			}
		case err != nil:
			return err
		default:
			if _, err := s.db.ExecContext(ctx,
				`UPDATE roles SET name = ?, description = ?, updated_at = ? WHERE id = ?`,
				tpl.Name, tpl.Description, nowUTC(), role.ID); err != nil {
				return fmt.Errorf("refresh template role %s: %w", tpl.Code, err)
			}
		}
		if err := s.ReplaceRolePermissions(ctx, role.ID, tpl.Permissions); err != nil {
			return fmt.Errorf("seed template role permissions %s: %w", tpl.Code, err)
		}
	}
	return nil
}

// RoleDrift reports a built-in role whose permission set no longer matches the
// current template because the tenant customized it.
//
// It exists because the safe upgrade rule ("never overwrite a tenant's edit") has
// a silent side effect: a permission added to a template never reaches a
// customized role, and nobody is told. Without this report the platform can ship
// a permission point that a subset of tenants can never grant — the same class of
// failure as a permission missing from the catalog, only per tenant.
type RoleDrift struct {
	TenantID string   `json:"tenantId"`
	Scope    Scope    `json:"scope"`
	RoleCode string   `json:"role"`
	Missing  []string `json:"missingPermissions"`
}

// EnsureTenantRoles clones any missing built-in role into the tenant and
// upgrades the permissions of un-customized built-in roles to match the latest
// template.
//
// A built-in role is considered "un-customized" when its current permission set
// is a subset of the template's: that means the tenant only ever held whatever
// the platform shipped (possibly an older template) and never added a custom
// permission. In that case the template is authoritative and any newly added
// permission is backfilled — this is how `tenant:workspace:join` reaches tenant
// instances created before the permission existed.
//
// When the tenant has granted a permission the template does not carry, the
// role is treated as customized and left untouched: the tenant's edits win. Any
// template permission such a role lacks is returned as drift so the caller can
// report it instead of the upgrade being silently skipped.
func (s *Store) EnsureTenantRoles(
	ctx context.Context, tenantID string,
) (created int, drift []RoleDrift, err error) {
	if tenantID == "" {
		return 0, nil, errors.New("tenant id is required")
	}
	existing, err := s.tenantRoleKeys(ctx, tenantID)
	if err != nil {
		return 0, nil, err
	}
	for _, tpl := range BuiltinRoleTemplates {
		key := string(tpl.Scope) + "|" + tpl.Code
		roleID, ok := existing[key]
		if !ok {
			roleID = newID()
			now := nowUTC()
			if _, err := s.db.ExecContext(ctx,
				`INSERT INTO roles
				     (id, tenant_id, scope, code, name, description, is_builtin, created_at, updated_at)
				 VALUES (?, ?, ?, ?, ?, ?, '1', ?, ?)`,
				roleID, tenantID, string(tpl.Scope), tpl.Code, tpl.Name, tpl.Description, now, now,
			); err != nil {
				if isUniqueViolation(err) {
					continue // Instantiated concurrently.
				}
				return created, drift, fmt.Errorf("instantiate role %s for tenant: %w", tpl.Code, err)
			}
			if err := s.ReplaceRolePermissions(ctx, roleID, tpl.Permissions); err != nil {
				return created, drift, fmt.Errorf("instantiate role permissions %s: %w", tpl.Code, err)
			}
			created++
			continue
		}

		// The role already exists: backfill any permission the current template
		// grants that the tenant is missing, but only when the tenant has not
		// customized the role. Customization is detected by the presence of a
		// permission the template does not carry.
		current, err := s.rolePermissionSet(ctx, roleID)
		if err != nil {
			return created, drift, fmt.Errorf("load role permissions %s: %w", tpl.Code, err)
		}
		templateSet := toSet(tpl.Permissions)
		customized := false
		for perm := range current {
			if !templateSet[perm] {
				customized = true
				break
			}
		}
		// Add every template permission the tenant lacks. Removal is deliberate:
		// never strip a permission the tenant granted, even if the template no
		// longer carries it — that would silently revoke access on upgrade.
		var missing []string
		for _, perm := range tpl.Permissions {
			if !current[perm] {
				missing = append(missing, perm)
			}
		}
		if customized {
			if len(missing) > 0 {
				drift = append(drift, RoleDrift{
					TenantID: tenantID,
					Scope:    tpl.Scope,
					RoleCode: tpl.Code,
					Missing:  missing,
				})
			}
			continue
		}
		if len(missing) == 0 {
			continue
		}
		if err := s.addRolePermissions(ctx, roleID, missing); err != nil {
			return created, drift, fmt.Errorf("upgrade role permissions %s: %w", tpl.Code, err)
		}
	}
	return created, drift, nil
}

// ReportBuiltinRoleDrift lists the built-in roles of one tenant that are missing
// a permission their current template grants, without changing anything.
//
// Read-only twin of the detection inside EnsureTenantRoles: an operator (or a
// migration) needs to see the backlog for a tenant on demand, not only as a log
// line emitted the first time the tenant was resolved in some process.
func (s *Store) ReportBuiltinRoleDrift(ctx context.Context, tenantID string) ([]RoleDrift, error) {
	if tenantID == "" {
		return nil, errors.New("tenant id is required")
	}
	existing, err := s.tenantRoleKeys(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	var drift []RoleDrift
	for _, tpl := range BuiltinRoleTemplates {
		roleID, ok := existing[string(tpl.Scope)+"|"+tpl.Code]
		if !ok {
			// Not instantiated yet: EnsureTenantRoles will clone the current
			// template, so there is nothing to report.
			continue
		}
		current, err := s.rolePermissionSet(ctx, roleID)
		if err != nil {
			return nil, fmt.Errorf("load role permissions %s: %w", tpl.Code, err)
		}
		var missing []string
		for _, perm := range tpl.Permissions {
			if !current[perm] {
				missing = append(missing, perm)
			}
		}
		if len(missing) > 0 {
			drift = append(drift, RoleDrift{
				TenantID: tenantID,
				Scope:    tpl.Scope,
				RoleCode: tpl.Code,
				Missing:  missing,
			})
		}
	}
	return drift, nil
}

// rolePermissionSet returns the set of permission codes granted to a role.
func (s *Store) rolePermissionSet(ctx context.Context, roleID string) (map[string]bool, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT permission_code FROM role_permissions WHERE role_id = ?`, roleID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	set := make(map[string]bool)
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil, err
		}
		set[code] = true
	}
	return set, rows.Err()
}

// addRolePermissions inserts permission codes that are not yet granted.
//
// Idempotency comes from the (role_id, permission_code) primary key rather
// than from dialect-specific upsert syntax: a duplicate raises a unique
// violation on all three backends, which is also how the permission and role
// seeders tolerate concurrent replica startup.
func (s *Store) addRolePermissions(ctx context.Context, roleID string, codes []string) error {
	now := nowUTC()
	for _, code := range codes {
		_, err := s.db.ExecContext(ctx,
			`INSERT INTO role_permissions (role_id, permission_code, created_at)
			 VALUES (?, ?, ?)`,
			roleID, code, now)
		if err != nil && !isUniqueViolation(err) {
			return err
		}
	}
	return nil
}

func toSet(items []string) map[string]bool {
	set := make(map[string]bool, len(items))
	for _, item := range items {
		set[item] = true
	}
	return set
}

// tenantRoleKeys returns the "scope|code" -> role id map of roles a tenant
// already owns.
func (s *Store) tenantRoleKeys(ctx context.Context, tenantID string) (map[string]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT scope, code, id FROM roles WHERE tenant_id = ?`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("query tenant roles: %w", err)
	}
	defer rows.Close()

	keys := make(map[string]string)
	for rows.Next() {
		var scope, code, id string
		if err := rows.Scan(&scope, &code, &id); err != nil {
			return nil, fmt.Errorf("scan tenant role: %w", err)
		}
		keys[scope+"|"+code] = id
	}
	return keys, rows.Err()
}

// ListRoles returns the tenant's roles (optionally filtered by scope) with
// their permission composition and how many bindings reference them.
func (s *Store) ListRoles(ctx context.Context, tenantID string, scope Scope) ([]Role, error) {
	query := `SELECT id, tenant_id, scope, code, name, description, is_builtin
	            FROM roles WHERE tenant_id = ?`
	args := []interface{}{tenantID}
	if scope != "" {
		query += ` AND scope = ?`
		args = append(args, string(scope))
	}
	query += ` ORDER BY scope, is_builtin DESC, code`

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query roles: %w", err)
	}
	defer rows.Close()

	roles := make([]Role, 0, len(BuiltinRoleTemplates))
	for rows.Next() {
		role, scanErr := scanRole(rows)
		if scanErr != nil {
			return nil, scanErr
		}
		roles = append(roles, *role)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Resolve permissions and member counts in bulk to avoid N+1 queries.
	perms, err := s.permissionsByRoleForTenant(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	counts, err := s.memberCountsByRoleCode(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	for i := range roles {
		roles[i].Permissions = perms[roles[i].ID]
		if roles[i].Permissions == nil {
			roles[i].Permissions = []string{}
		}
		roles[i].MemberCount = counts[string(roles[i].Scope)+"|"+roles[i].Code]
	}
	return roles, nil
}

// GetRoleByCode loads one tenant role by its scope and code.
func (s *Store) GetRoleByCode(ctx context.Context, tenantID string, scope Scope, code string) (*Role, error) {
	role, err := s.getRole(ctx, tenantID, scope, code)
	if err != nil {
		return nil, err
	}
	role.Permissions, err = s.RolePermissions(ctx, role.ID)
	if err != nil {
		return nil, err
	}
	return role, nil
}

// GetRoleByID loads one tenant role by primary key, scoped to the tenant so a
// caller can never address another tenant's role.
func (s *Store) GetRoleByID(ctx context.Context, tenantID, roleID string) (*Role, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, tenant_id, scope, code, name, description, is_builtin
		   FROM roles WHERE id = ? AND tenant_id = ?`, roleID, tenantID)
	role, err := scanRole(row)
	if err != nil {
		return nil, err
	}
	role.Permissions, err = s.RolePermissions(ctx, role.ID)
	if err != nil {
		return nil, err
	}
	return role, nil
}

func (s *Store) getRole(ctx context.Context, tenantID string, scope Scope, code string) (*Role, error) {
	return scanRole(s.db.QueryRowContext(ctx,
		`SELECT id, tenant_id, scope, code, name, description, is_builtin
		   FROM roles WHERE tenant_id = ? AND scope = ? AND code = ?`,
		tenantID, string(scope), code))
}

// rowScanner abstracts *sql.Row and *sql.Rows for shared scanning.
type rowScanner interface {
	Scan(dest ...interface{}) error
}

func scanRole(row rowScanner) (*Role, error) {
	var (
		role      Role
		scope     string
		isBuiltin string
	)
	err := row.Scan(&role.ID, &role.TenantID, &scope, &role.Code,
		&role.Name, &role.Description, &isBuiltin)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrRoleNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan role: %w", err)
	}
	role.Scope = Scope(scope)
	role.IsBuiltin = isBuiltin == "1"
	return &role, nil
}

// CreateRole inserts a tenant-owned custom role with its permission set.
func (s *Store) CreateRole(
	ctx context.Context, tenantID string, scope Scope, code, name, description string, permissions []string,
) (*Role, error) {
	roleID := newID()
	now := nowUTC()
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO roles
		     (id, tenant_id, scope, code, name, description, is_builtin, created_at, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, '0', ?, ?)`,
		roleID, tenantID, string(scope), code, name, description, now, now,
	); err != nil {
		if isUniqueViolation(err) {
			return nil, ErrRoleExists
		}
		return nil, fmt.Errorf("insert role: %w", err)
	}
	if err := s.ReplaceRolePermissions(ctx, roleID, permissions); err != nil {
		return nil, err
	}
	return s.GetRoleByID(ctx, tenantID, roleID)
}

// UpdateRole renames a role and replaces its permission composition. The role
// code is immutable so existing bindings stay valid.
func (s *Store) UpdateRole(ctx context.Context, tenantID, roleID, name, description string, permissions []string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE roles SET name = ?, description = ?, updated_at = ?
		  WHERE id = ? AND tenant_id = ?`,
		name, description, nowUTC(), roleID, tenantID)
	if err != nil {
		return fmt.Errorf("update role: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		// Either the role does not exist or it belongs to another tenant;
		// both are reported as not found so nothing about other tenants leaks.
		if _, getErr := s.GetRoleByID(ctx, tenantID, roleID); getErr != nil {
			return getErr
		}
	}
	return s.ReplaceRolePermissions(ctx, roleID, permissions)
}

// DeleteRole removes a tenant-owned role and its permission rows.
func (s *Store) DeleteRole(ctx context.Context, tenantID, roleID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin delete role tx: %w", err)
	}
	defer tx.Rollback() // no-op after a successful Commit

	res, err := tx.ExecContext(ctx,
		`DELETE FROM roles WHERE id = ? AND tenant_id = ?`, roleID, tenantID)
	if err != nil {
		return fmt.Errorf("delete role: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		return ErrRoleNotFound
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM role_permissions WHERE role_id = ?`, roleID); err != nil {
		return fmt.Errorf("delete role permissions: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit delete role tx: %w", err)
	}
	return nil
}

// ReplaceRolePermissions atomically swaps the permission set of a role.
func (s *Store) ReplaceRolePermissions(ctx context.Context, roleID string, permissions []string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin role permissions tx: %w", err)
	}
	defer tx.Rollback() // no-op after a successful Commit

	if _, err := tx.ExecContext(ctx,
		`DELETE FROM role_permissions WHERE role_id = ?`, roleID); err != nil {
		return fmt.Errorf("clear role permissions: %w", err)
	}
	now := nowUTC()
	for _, code := range dedupe(permissions) {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO role_permissions (role_id, permission_code, created_at)
			 VALUES (?, ?, ?)`, roleID, code, now); err != nil {
			return fmt.Errorf("insert role permission %s: %w", code, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit role permissions tx: %w", err)
	}
	return nil
}

// RolePermissions returns the permission codes granted to a role.
func (s *Store) RolePermissions(ctx context.Context, roleID string) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT permission_code FROM role_permissions WHERE role_id = ? ORDER BY permission_code`,
		roleID)
	if err != nil {
		return nil, fmt.Errorf("query role permissions: %w", err)
	}
	defer rows.Close()

	codes := make([]string, 0, len(Catalog))
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil, fmt.Errorf("scan role permission: %w", err)
		}
		codes = append(codes, code)
	}
	return codes, rows.Err()
}

// PermissionsByRoleCode resolves the effective permission codes of one role
// identified by (tenant, scope, code). Returns an empty slice when the role
// does not exist, so an unknown role simply grants nothing.
func (s *Store) PermissionsByRoleCode(ctx context.Context, tenantID string, scope Scope, code string) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT rp.permission_code
		   FROM roles r
		   JOIN role_permissions rp ON rp.role_id = r.id
		  WHERE r.tenant_id = ? AND r.scope = ? AND r.code = ?`,
		tenantID, string(scope), code)
	if err != nil {
		return nil, fmt.Errorf("query permissions by role code: %w", err)
	}
	defer rows.Close()

	codes := make([]string, 0, len(Catalog))
	for rows.Next() {
		var perm string
		if err := rows.Scan(&perm); err != nil {
			return nil, fmt.Errorf("scan permission by role code: %w", err)
		}
		codes = append(codes, perm)
	}
	return codes, rows.Err()
}

// permissionsByRoleForTenant returns role_id -> permission codes for one tenant.
func (s *Store) permissionsByRoleForTenant(ctx context.Context, tenantID string) (map[string][]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT rp.role_id, rp.permission_code
		   FROM role_permissions rp
		   JOIN roles r ON r.id = rp.role_id
		  WHERE r.tenant_id = ?
		  ORDER BY rp.permission_code`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("query tenant role permissions: %w", err)
	}
	defer rows.Close()

	byRole := make(map[string][]string)
	for rows.Next() {
		var roleID, code string
		if err := rows.Scan(&roleID, &code); err != nil {
			return nil, fmt.Errorf("scan tenant role permission: %w", err)
		}
		byRole[roleID] = append(byRole[roleID], code)
	}
	return byRole, rows.Err()
}

// memberCountsByRoleCode counts bindings per "scope|code" inside one tenant so
// the UI can warn before a role is deleted.
func (s *Store) memberCountsByRoleCode(ctx context.Context, tenantID string) (map[string]int, error) {
	counts := make(map[string]int)

	tenantRows, err := s.db.QueryContext(ctx,
		`SELECT role, COUNT(*) FROM tenant_members WHERE tenant_id = ? GROUP BY role`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("count tenant role bindings: %w", err)
	}
	defer tenantRows.Close()
	for tenantRows.Next() {
		var (
			code  string
			total int
		)
		if err := tenantRows.Scan(&code, &total); err != nil {
			return nil, fmt.Errorf("scan tenant role binding count: %w", err)
		}
		counts[string(ScopeTenant)+"|"+code] = total
	}
	if err := tenantRows.Err(); err != nil {
		return nil, err
	}

	wsRows, err := s.db.QueryContext(ctx,
		`SELECT role_code, COUNT(*) FROM workspace_members WHERE tenant_id = ? GROUP BY role_code`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("count workspace role bindings: %w", err)
	}
	defer wsRows.Close()
	for wsRows.Next() {
		var (
			code  string
			total int
		)
		if err := wsRows.Scan(&code, &total); err != nil {
			return nil, fmt.Errorf("scan workspace role binding count: %w", err)
		}
		counts[string(ScopeWorkspace)+"|"+code] = total
	}
	return counts, wsRows.Err()
}

// ---------------------------------------------------------------------------
// Workspace role bindings
// ---------------------------------------------------------------------------

// UpsertWorkspaceMember grants or updates a workspace role binding.
func (s *Store) UpsertWorkspaceMember(
	ctx context.Context, tenantID, workspaceID, userID, roleCode, grantedBy string,
) error {
	now := nowUTC()
	res, err := s.db.ExecContext(ctx,
		`UPDATE workspace_members
		    SET role_code = ?, granted_by = ?, updated_at = ?
		  WHERE workspace_id = ? AND user_id = ?`,
		roleCode, grantedBy, now, workspaceID, userID)
	if err != nil {
		return fmt.Errorf("update workspace member: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected > 0 {
		return nil
	}
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO workspace_members
		     (id, tenant_id, workspace_id, principal_type, user_id, role_code, granted_by, created_at, updated_at)
		 VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?)`,
		newID(), tenantID, workspaceID, userID, roleCode, grantedBy, now, now,
	); err != nil {
		return fmt.Errorf("insert workspace member: %w", err)
	}
	return nil
}

// WorkspaceRole returns the role a user holds in one workspace.
func (s *Store) WorkspaceRole(ctx context.Context, tenantID, workspaceID, userID string) (string, error) {
	var roleCode string
	err := s.db.QueryRowContext(ctx,
		`SELECT role_code FROM workspace_members
		  WHERE tenant_id = ? AND workspace_id = ? AND user_id = ?`,
		tenantID, workspaceID, userID).Scan(&roleCode)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrBindingNotFound
	}
	if err != nil {
		return "", fmt.Errorf("query workspace role: %w", err)
	}
	return roleCode, nil
}

// CountWorkspaceOwners returns the number of owners bound to a workspace, used
// to protect the last owner from being removed or downgraded.
func (s *Store) CountWorkspaceOwners(ctx context.Context, tenantID, workspaceID string) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM workspace_members
		  WHERE tenant_id = ? AND workspace_id = ? AND role_code = ?`,
		tenantID, workspaceID, RoleWorkspaceOwner).Scan(&count)
	if err != nil {
		return 0, fmt.Errorf("count workspace owners: %w", err)
	}
	return count, nil
}

// ListWorkspaceRolesForUser returns workspace_id -> role_code for every
// workspace the user can access inside a tenant. One query, no N+1.
func (s *Store) ListWorkspaceRolesForUser(ctx context.Context, tenantID, userID string) (map[string]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT workspace_id, role_code FROM workspace_members
		  WHERE tenant_id = ? AND user_id = ?`, tenantID, userID)
	if err != nil {
		return nil, fmt.Errorf("query user workspace roles: %w", err)
	}
	defer rows.Close()

	roles := make(map[string]string)
	for rows.Next() {
		var workspaceID, roleCode string
		if err := rows.Scan(&workspaceID, &roleCode); err != nil {
			return nil, fmt.Errorf("scan user workspace role: %w", err)
		}
		roles[workspaceID] = roleCode
	}
	return roles, rows.Err()
}

// ListWorkspaceMembers returns every binding of one workspace.
func (s *Store) ListWorkspaceMembers(ctx context.Context, tenantID, workspaceID string) ([]WorkspaceMember, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT tenant_id, workspace_id, user_id, role_code, granted_by, created_at
		   FROM workspace_members
		  WHERE tenant_id = ? AND workspace_id = ?
		  ORDER BY created_at ASC`, tenantID, workspaceID)
	if err != nil {
		return nil, fmt.Errorf("query workspace members: %w", err)
	}
	defer rows.Close()

	members := make([]WorkspaceMember, 0, 8)
	for rows.Next() {
		var m WorkspaceMember
		if err := rows.Scan(&m.TenantID, &m.WorkspaceID, &m.UserID,
			&m.RoleCode, &m.GrantedBy, &m.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan workspace member: %w", err)
		}
		members = append(members, m)
	}
	return members, rows.Err()
}

// RemoveWorkspaceMember revokes a single workspace binding.
func (s *Store) RemoveWorkspaceMember(ctx context.Context, tenantID, workspaceID, userID string) error {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM workspace_members
		  WHERE tenant_id = ? AND workspace_id = ? AND user_id = ?`,
		tenantID, workspaceID, userID)
	if err != nil {
		return fmt.Errorf("delete workspace member: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		return ErrBindingNotFound
	}
	return nil
}

// RemoveWorkspaceBindings drops every binding of a deleted workspace.
func (s *Store) RemoveWorkspaceBindings(ctx context.Context, tenantID, workspaceID string) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM workspace_members WHERE tenant_id = ? AND workspace_id = ?`,
		tenantID, workspaceID)
	if err != nil {
		return 0, fmt.Errorf("delete workspace bindings: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// RemoveTenantUserBindings cascades a tenant membership revocation to every
// workspace binding the user held inside that tenant.
func (s *Store) RemoveTenantUserBindings(ctx context.Context, tenantID, userID string) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM workspace_members WHERE tenant_id = ? AND user_id = ?`, tenantID, userID)
	if err != nil {
		return 0, fmt.Errorf("delete tenant user bindings: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

// WriteAudit appends an authorization change record. Failures are reported to
// the caller, which logs them without failing the originating request.
func (s *Store) WriteAudit(ctx context.Context, entry AuditEntry) error {
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO rbac_audit_log
		     (id, tenant_id, actor_id, action, scope_type, scope_id, target_id, detail, created_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		newID(), entry.TenantID, entry.ActorID, entry.Action, entry.ScopeType,
		entry.ScopeID, entry.TargetID, truncate(entry.Detail, 512),
		formatUTC(time.Now()),
	); err != nil {
		return fmt.Errorf("insert rbac audit: %w", err)
	}
	return nil
}

// truncate keeps a stored detail string within its column width.
func truncate(value string, max int) string {
	if len(value) <= max {
		return value
	}
	return value[:max]
}
