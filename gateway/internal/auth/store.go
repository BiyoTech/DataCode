// Package auth implements the gateway-owned authentication domain:
// identities (users), tenants, tenant memberships and refresh tokens.
//
// Identity and membership are deliberately separated: a single user account
// (unique username + password) may own its own tenant (primary account) and
// at the same time be a member of other tenants (sub account). The tenant
// context is therefore chosen at login time, not derived from the identity.
//
// The store supports both SQLite (zero-dependency dev) and MySQL (production).
// The dialect is selected via the DSN passed to NewStore.
package auth

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql" // MySQL driver
	"go.uber.org/zap"
	_ "modernc.org/sqlite" // pure-Go SQLite driver (no CGO)
)

// Dialect identifies the database backend in use.
type Dialect string

const (
	DialectSQLite Dialect = "sqlite"
	DialectMySQL  Dialect = "mysql"
)

// Sentinel errors returned by the store and mapped to HTTP codes by handlers.
var (
	ErrUserNotFound       = errors.New("user not found")
	ErrUsernameTaken      = errors.New("username already taken")
	ErrMembershipNotFound = errors.New("membership not found")
	ErrMembershipExists   = errors.New("membership already exists")
	ErrTokenNotFound      = errors.New("refresh token not found")
)

// Role values used when a tenant is created. Every other role is defined in
// the database by the RBAC domain, which is the authority on role validity.
const (
	RoleAdmin     = "admin"
	RoleDeveloper = "developer"
	RoleViewer    = "viewer"
)

// User is an authentication identity. It is tenant-agnostic.
type User struct {
	ID           string
	Username     string
	PasswordHash string
	Status       string
	LastTenantID string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

// Tenant is an organization workspace grouping accounts.
type Tenant struct {
	ID        string
	Name      string
	CreatedBy string
	CreatedAt time.Time
}

// Membership links a user to a tenant with a role. IsOwner marks the tenant
// the user created (the "primary account" in the identity picker).
type Membership struct {
	TenantID   string
	TenantName string
	UserID     string
	Username   string
	Role       string
	IsOwner    bool
	CreatedAt  time.Time
}

// RefreshToken is a persisted, hashed refresh credential bound to a
// (user, tenant) identity pair.
type RefreshToken struct {
	ID        string
	UserID    string
	TenantID  string
	TokenHash string
	ExpiresAt time.Time
	RevokedAt *time.Time
	CreatedAt time.Time
}

// Store is the persistence layer for the auth domain.
// It supports both SQLite and MySQL backends.
// All SQL uses bound parameters and all timestamps are stored in UTC.
type Store struct {
	db      *sql.DB
	dialect Dialect
}

// NewStore opens (and creates if missing) the auth database using the given
// DSN and dialect, then applies the schema.
//
// For SQLite, dsn is a file path (e.g. ".data/gateway.db").
// For MySQL, dsn is a go-sql-driver/mysql DSN
// (e.g. "user:password@tcp(host:3306)/dbname?parseTime=true").
func NewStore(dialect Dialect, dsn string) (*Store, error) {
	if dsn == "" {
		return nil, errors.New("auth dsn is empty")
	}

	driverName, err := openDB(dialect, dsn)
	if err != nil {
		return nil, err
	}

	db, err := sql.Open(driverName, dsn)
	if err != nil {
		return nil, fmt.Errorf("open auth db: %w", err)
	}

	// SQLite handles a single writer; keep the pool small and predictable.
	// MySQL can handle concurrent connections; use a reasonable pool.
	if dialect == DialectSQLite {
		db.SetMaxOpenConns(1)
	} else {
		db.SetMaxOpenConns(10)
		db.SetMaxIdleConns(5)
	}

	// Verify connectivity.
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping auth db: %w", err)
	}

	store := &Store{db: db, dialect: dialect}
	if err := store.migrate(context.Background()); err != nil {
		db.Close()
		return nil, err
	}
	return store, nil
}

// openDB performs any pre-open setup (e.g. creating directories for SQLite)
// and returns the driver name to pass to sql.Open.
func openDB(dialect Dialect, dsn string) (string, error) {
	switch dialect {
	case DialectSQLite:
		if dir := filepath.Dir(dsn); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o750); err != nil {
				return "", fmt.Errorf("create auth db directory: %w", err)
			}
		}
		return "sqlite", nil
	case DialectMySQL:
		return "mysql", nil
	default:
		return "", fmt.Errorf("unsupported auth dialect: %s", dialect)
	}
}

// Close releases the underlying database handle.
func (s *Store) Close() error {
	return s.db.Close()
}

// Dialect returns the database dialect this store was initialized with.
func (s *Store) Dialect() Dialect { return s.dialect }

// DB exposes the underlying handle so sibling domains (e.g. rbac) can reuse
// the same connection pool and transaction capabilities instead of opening a
// second one against the same database.
func (s *Store) DB() *sql.DB { return s.db }

// migrate creates the schema if it does not exist yet.
// The schema uses cross-dialect-compatible SQL where possible, with
// dialect-specific branches for CREATE INDEX (MySQL does not support IF NOT EXISTS).
func (s *Store) migrate(ctx context.Context) error {
	// Reclaim `workspace_members`, whose ownership moved to the gateway while a
	// table with the previous owner's shape may still live in the shared
	// database. `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table,
	// so the stale shape would survive forever and every query would fail on a
	// missing column. Existing bindings are carried over instead of discarded,
	// otherwise members — including workspace owners — would lose access.
	carried, err := s.takeLegacyWorkspaceBindings(ctx)
	if err != nil {
		return fmt.Errorf("reclaim legacy workspace_members: %w", err)
	}

	// Schema DDL statements — executed one by one because MySQL does not
	// support multi-statement execution via a single Exec call.
	// Both SQLite and MySQL support: VARCHAR type, UNIQUE constraint,
	// CREATE TABLE IF NOT EXISTS.
	// MySQL does NOT support CREATE INDEX IF NOT EXISTS — handled separately.
	ddl := []string{
		`CREATE TABLE IF NOT EXISTS users (
    id             VARCHAR(64) NOT NULL PRIMARY KEY,
    username       VARCHAR(64) NOT NULL,
    password_hash  TEXT NOT NULL,
    status         VARCHAR(32) NOT NULL DEFAULT 'active',
    last_tenant_id VARCHAR(64) NOT NULL DEFAULT '',
    created_at     VARCHAR(64) NOT NULL,
    updated_at     VARCHAR(64) NOT NULL,
    UNIQUE (username)
)`,
		`CREATE TABLE IF NOT EXISTS tenants (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL
)`,
		`CREATE TABLE IF NOT EXISTS tenant_members (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    tenant_id  VARCHAR(64) NOT NULL,
    user_id    VARCHAR(64) NOT NULL,
    role       VARCHAR(32) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE (tenant_id, user_id)
)`,
		`CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    user_id    VARCHAR(64) NOT NULL,
    tenant_id  VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    revoked_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    UNIQUE (token_hash)
)`,
		// -------------------------------------------------------------------
		// RBAC schema. The gateway is the platform's single authorization
		// center: permission catalog, roles, role composition and workspace
		// role bindings all live here.
		// -------------------------------------------------------------------
		// Global permission catalog. Rows are synchronized from the code-side
		// catalog on every startup and are never mutated through the API.
		`CREATE TABLE IF NOT EXISTS permissions (
    code        VARCHAR(96)  NOT NULL PRIMARY KEY,
    scope       VARCHAR(32)  NOT NULL,
    resource    VARCHAR(64)  NOT NULL,
    action      VARCHAR(32)  NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    deprecated  VARCHAR(8)   NOT NULL DEFAULT '0',
    created_at  VARCHAR(64)  NOT NULL,
    updated_at  VARCHAR(64)  NOT NULL
)`,
		// Roles. tenant_id = '' marks a SYSTEM TEMPLATE, written only by the
		// seeder; tenant_id = <tid> marks a tenant-owned instance which the
		// seeder never touches. That separation is what makes the permission
		// set of a built-in role tenant-editable without being reverted on
		// the next startup.
		`CREATE TABLE IF NOT EXISTS roles (
    id          VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id   VARCHAR(64)  NOT NULL DEFAULT '',
    scope       VARCHAR(32)  NOT NULL,
    code        VARCHAR(64)  NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    is_builtin  VARCHAR(8)   NOT NULL DEFAULT '0',
    created_at  VARCHAR(64)  NOT NULL,
    updated_at  VARCHAR(64)  NOT NULL,
    UNIQUE (tenant_id, scope, code)
)`,
		`CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         VARCHAR(64) NOT NULL,
    permission_code VARCHAR(96) NOT NULL,
    created_at      VARCHAR(64) NOT NULL,
    PRIMARY KEY (role_id, permission_code)
)`,
		// Workspace role bindings. tenant_id is carried explicitly so
		// cross-tenant checks never need a join.
		`CREATE TABLE IF NOT EXISTS workspace_members (
    id             VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id      VARCHAR(64)  NOT NULL,
    workspace_id   VARCHAR(128) NOT NULL,
    principal_type VARCHAR(32)  NOT NULL DEFAULT 'user',
    user_id        VARCHAR(64)  NOT NULL,
    role_code      VARCHAR(64)  NOT NULL,
    granted_by     VARCHAR(64)  NOT NULL DEFAULT '',
    created_at     VARCHAR(64)  NOT NULL,
    updated_at     VARCHAR(64)  NOT NULL,
    UNIQUE (workspace_id, user_id)
)`,
		`CREATE TABLE IF NOT EXISTS rbac_audit_log (
    id         VARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL,
    actor_id   VARCHAR(64)  NOT NULL,
    action     VARCHAR(64)  NOT NULL,
    scope_type VARCHAR(32)  NOT NULL,
    scope_id   VARCHAR(128) NOT NULL DEFAULT '',
    target_id  VARCHAR(128) NOT NULL DEFAULT '',
    detail     VARCHAR(512) NOT NULL DEFAULT '',
    created_at VARCHAR(64)  NOT NULL
)`,
	}
	for _, stmt := range ddl {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("apply auth schema (%s): %w", stmt[:40], err)
		}
	}

	// Create indexes — dialect-specific.
	// SQLite supports CREATE INDEX IF NOT EXISTS; MySQL does not.
	indexes := []string{
		"idx_tenant_members_user ON tenant_members (user_id)",
		"idx_tenant_members_tenant ON tenant_members (tenant_id)",
		"idx_refresh_tokens_user ON refresh_tokens (user_id)",
		"idx_roles_tenant_scope ON roles (tenant_id, scope)",
		"idx_role_permissions_role ON role_permissions (role_id)",
		"idx_workspace_members_user ON workspace_members (user_id, tenant_id)",
		"idx_workspace_members_ws ON workspace_members (workspace_id)",
		"idx_rbac_audit_tenant ON rbac_audit_log (tenant_id, created_at)",
	}
	for _, idx := range indexes {
		if err := s.createIndexIfNotExists(ctx, idx); err != nil {
			return fmt.Errorf("create index %s: %w", idx, err)
		}
	}

	// The reclaimed bindings can only be written once the current shape exists.
	if err := s.restoreWorkspaceBindings(ctx, carried); err != nil {
		return fmt.Errorf("restore reclaimed workspace bindings: %w", err)
	}
	return nil
}

// legacyWorkspaceBinding is a workspace role binding read from the shape used
// before the gateway became the platform's authorization center.
type legacyWorkspaceBinding struct {
	tenantID    string
	workspaceID string
	userID      string
	roleCode    string
}

// takeLegacyWorkspaceBindings detects a `workspace_members` table with the
// previous owner's shape (workspace_id, user_id, role), reads its rows and drops
// it so the current shape can be created.
//
// The legacy `role` values are the codes of the built-in workspace roles
// (workspace_owner / engineer / analyst / viewer), which the gateway seeds under
// the same codes, so they carry over unchanged. The tenant is recovered from the
// workspace identifier convention "ws-{tenant_id}-{slug}".
//
// Returns nil when there is nothing to reclaim, which is the steady state.
func (s *Store) takeLegacyWorkspaceBindings(ctx context.Context) ([]legacyWorkspaceBinding, error) {
	const table = "workspace_members"

	exists, err := s.tableExists(ctx, table)
	if err != nil || !exists {
		return nil, err
	}
	// The discriminating column of the current shape: when present, the table is
	// already owned by the gateway and must be left untouched.
	current, err := s.columnExists(ctx, table, "role_code")
	if err != nil || current {
		return nil, err
	}

	rows, err := s.db.QueryContext(ctx,
		`SELECT workspace_id, user_id, role FROM `+table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var (
		carried []legacyWorkspaceBinding
		skipped int
	)
	for rows.Next() {
		var binding legacyWorkspaceBinding
		if err := rows.Scan(&binding.workspaceID, &binding.userID, &binding.roleCode); err != nil {
			return nil, err
		}
		tenantID, ok := tenantIDFromWorkspaceID(binding.workspaceID)
		if !ok {
			// Without a tenant the binding cannot be scoped safely, and an
			// unscoped binding would be a cross-tenant access risk.
			skipped++
			continue
		}
		binding.tenantID = tenantID
		carried = append(carried, binding)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	if _, err := s.db.ExecContext(ctx, "DROP TABLE "+table); err != nil {
		return nil, err
	}
	zap.L().Warn("legacy_workspace_members_reclaimed",
		zap.Int("carried", len(carried)),
		zap.Int("skipped", skipped))
	return carried, nil
}

// restoreWorkspaceBindings writes reclaimed bindings into the current shape.
// Conflicting rows are skipped: a binding already granted through the gateway
// is authoritative.
func (s *Store) restoreWorkspaceBindings(ctx context.Context, bindings []legacyWorkspaceBinding) error {
	if len(bindings) == 0 {
		return nil
	}
	now := formatUTC(time.Now().UTC())
	for _, binding := range bindings {
		_, err := s.db.ExecContext(ctx,
			`INSERT INTO workspace_members
			     (id, tenant_id, workspace_id, principal_type, user_id, role_code,
			      granted_by, created_at, updated_at)
			 VALUES (?, ?, ?, 'user', ?, ?, '', ?, ?)`,
			newID(), binding.tenantID, binding.workspaceID,
			binding.userID, binding.roleCode, now, now)
		if err != nil && !isUniqueViolation(err) {
			return err
		}
	}
	zap.L().Info("workspace_bindings_restored", zap.Int("count", len(bindings)))
	return nil
}

// tenantIDFromWorkspaceID recovers the tenant from a workspace identifier,
// which the workspace domain mints as "ws-{tenant_id}-{slug}" with a tenant id
// that never contains a dash.
func tenantIDFromWorkspaceID(workspaceID string) (string, bool) {
	const prefix = "ws-"
	if !strings.HasPrefix(workspaceID, prefix) {
		return "", false
	}
	rest := workspaceID[len(prefix):]
	idx := strings.Index(rest, "-")
	if idx <= 0 {
		return "", false
	}
	return rest[:idx], true
}

// tableExists reports whether a table is present in the current database.
func (s *Store) tableExists(ctx context.Context, table string) (bool, error) {
	query := `SELECT COUNT(*) FROM information_schema.tables
	           WHERE table_schema = DATABASE() AND table_name = ?`
	if s.dialect == DialectSQLite {
		query = `SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?`
	}
	var count int
	if err := s.db.QueryRowContext(ctx, query, table).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

// columnExists reports whether a table carries a given column.
func (s *Store) columnExists(ctx context.Context, table, column string) (bool, error) {
	query := `SELECT COUNT(*) FROM information_schema.columns
	           WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?`
	if s.dialect == DialectSQLite {
		query = `SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?`
	}
	var count int
	if err := s.db.QueryRowContext(ctx, query, table, column).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

// createIndexIfNotExists creates an index, handling dialect differences.
// SQLite: uses CREATE INDEX IF NOT EXISTS.
// MySQL: checks INFORMATION_SCHEMA first (idempotent).
func (s *Store) createIndexIfNotExists(ctx context.Context, indexDef string) error {
	parts := strings.SplitN(indexDef, " ON ", 2)
	indexName := strings.TrimSpace(parts[0])
	tableAndCols := "ON " + strings.TrimSpace(parts[1])

	if s.dialect == DialectSQLite {
		_, err := s.db.ExecContext(ctx, fmt.Sprintf("CREATE INDEX IF NOT EXISTS %s %s", indexName, tableAndCols))
		return err
	}

	// MySQL: check if index already exists.
	var count int
	tableName := strings.SplitN(tableAndCols, " ", 3)[1]
	checkErr := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM information_schema.statistics
		 WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?`,
		tableName, indexName).Scan(&count)
	if checkErr != nil {
		return checkErr
	}
	if count > 0 {
		return nil // Index already exists.
	}
	_, err := s.db.ExecContext(ctx, fmt.Sprintf("CREATE INDEX %s %s", indexName, tableAndCols))
	return err
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

// GetUserByUsername loads a user by its unique username.
func (s *Store) GetUserByUsername(ctx context.Context, username string) (*User, error) {
	return s.scanUser(s.db.QueryRowContext(ctx,
		`SELECT id, username, password_hash, status, last_tenant_id, created_at, updated_at
		 FROM users WHERE username = ?`, username))
}

// GetUserByID loads a user by its primary key.
func (s *Store) GetUserByID(ctx context.Context, userID string) (*User, error) {
	return s.scanUser(s.db.QueryRowContext(ctx,
		`SELECT id, username, password_hash, status, last_tenant_id, created_at, updated_at
		 FROM users WHERE id = ?`, userID))
}

func (s *Store) scanUser(row *sql.Row) (*User, error) {
	var (
		u                    User
		createdAt, updatedAt string
	)
	err := row.Scan(&u.ID, &u.Username, &u.PasswordHash, &u.Status,
		&u.LastTenantID, &createdAt, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrUserNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan user: %w", err)
	}
	u.CreatedAt = parseUTC(createdAt)
	u.UpdatedAt = parseUTC(updatedAt)
	return &u, nil
}

// CreateUser inserts a standalone identity (used when a tenant admin adds a
// member whose username does not exist yet).
func (s *Store) CreateUser(ctx context.Context, username, passwordHash string) (*User, error) {
	now := time.Now().UTC()
	user := &User{
		ID:           newID(),
		Username:     username,
		PasswordHash: passwordHash,
		Status:       "active",
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO users (id, username, password_hash, status, last_tenant_id, created_at, updated_at)
		 VALUES (?, ?, ?, ?, '', ?, ?)`,
		user.ID, user.Username, user.PasswordHash, user.Status,
		formatUTC(now), formatUTC(now))
	if err != nil {
		if isUniqueViolation(err) {
			return nil, ErrUsernameTaken
		}
		return nil, fmt.Errorf("insert user: %w", err)
	}
	return user, nil
}

// UpdatePasswordHash replaces a user's password hash.
func (s *Store) UpdatePasswordHash(ctx context.Context, userID, passwordHash string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?`,
		passwordHash, formatUTC(time.Now().UTC()), userID)
	if err != nil {
		return fmt.Errorf("update password hash: %w", err)
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrUserNotFound
	}
	return nil
}

// SetLastTenant records the tenant identity a user most recently entered,
// used to highlight the "last used" identity in the picker.
func (s *Store) SetLastTenant(ctx context.Context, userID, tenantID string) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE users SET last_tenant_id = ?, updated_at = ? WHERE id = ?`,
		tenantID, formatUTC(time.Now().UTC()), userID)
	if err != nil {
		return fmt.Errorf("update last tenant: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Registration (atomic: user + tenant + admin membership)
// ---------------------------------------------------------------------------

// RegisterTenant creates an identity, its tenant and the owning admin
// membership in a single transaction.
func (s *Store) RegisterTenant(
	ctx context.Context, username, passwordHash, tenantName string,
) (*User, *Membership, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, nil, fmt.Errorf("begin register tx: %w", err)
	}
	defer tx.Rollback() // no-op after a successful Commit

	now := time.Now().UTC()
	nowStr := formatUTC(now)

	user := &User{
		ID:           newID(),
		Username:     username,
		PasswordHash: passwordHash,
		Status:       "active",
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	tenantID := newID()

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO users (id, username, password_hash, status, last_tenant_id, created_at, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		user.ID, user.Username, user.PasswordHash, user.Status, tenantID, nowStr, nowStr,
	); err != nil {
		if isUniqueViolation(err) {
			return nil, nil, ErrUsernameTaken
		}
		return nil, nil, fmt.Errorf("insert user: %w", err)
	}
	user.LastTenantID = tenantID

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO tenants (id, name, created_by, created_at) VALUES (?, ?, ?, ?)`,
		tenantID, tenantName, user.ID, nowStr,
	); err != nil {
		return nil, nil, fmt.Errorf("insert tenant: %w", err)
	}

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO tenant_members (id, tenant_id, user_id, role, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		newID(), tenantID, user.ID, RoleAdmin, nowStr,
	); err != nil {
		return nil, nil, fmt.Errorf("insert membership: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return nil, nil, fmt.Errorf("commit register tx: %w", err)
	}

	return user, &Membership{
		TenantID:   tenantID,
		TenantName: tenantName,
		UserID:     user.ID,
		Username:   user.Username,
		Role:       RoleAdmin,
		IsOwner:    true,
		CreatedAt:  now,
	}, nil
}

// ---------------------------------------------------------------------------
// Memberships
// ---------------------------------------------------------------------------

// ListMembershipsByUser returns every tenant identity available to a user,
// owned tenants first so the picker can render primary accounts on top.
func (s *Store) ListMembershipsByUser(ctx context.Context, userID string) ([]Membership, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT m.tenant_id, t.name, m.user_id, m.role, m.created_at,
		        CASE WHEN t.created_by = m.user_id THEN 1 ELSE 0 END AS is_owner
		 FROM tenant_members m
		 JOIN tenants t ON t.id = m.tenant_id
		 WHERE m.user_id = ?
		 ORDER BY is_owner DESC, m.created_at ASC`, userID)
	if err != nil {
		return nil, fmt.Errorf("query memberships: %w", err)
	}
	defer rows.Close()

	var memberships []Membership
	for rows.Next() {
		var (
			m         Membership
			createdAt string
			isOwner   int
		)
		if err := rows.Scan(&m.TenantID, &m.TenantName, &m.UserID, &m.Role,
			&createdAt, &isOwner); err != nil {
			return nil, fmt.Errorf("scan membership: %w", err)
		}
		m.CreatedAt = parseUTC(createdAt)
		m.IsOwner = isOwner == 1
		memberships = append(memberships, m)
	}
	return memberships, rows.Err()
}

// GetMembership loads a single (tenant, user) membership.
func (s *Store) GetMembership(ctx context.Context, tenantID, userID string) (*Membership, error) {
	var (
		m         Membership
		createdAt string
		isOwner   int
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT m.tenant_id, t.name, m.user_id, m.role, m.created_at,
		        CASE WHEN t.created_by = m.user_id THEN 1 ELSE 0 END AS is_owner
		 FROM tenant_members m
		 JOIN tenants t ON t.id = m.tenant_id
		 WHERE m.tenant_id = ? AND m.user_id = ?`, tenantID, userID,
	).Scan(&m.TenantID, &m.TenantName, &m.UserID, &m.Role, &createdAt, &isOwner)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMembershipNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan membership: %w", err)
	}
	m.CreatedAt = parseUTC(createdAt)
	m.IsOwner = isOwner == 1
	return &m, nil
}

// ListMembersByTenant returns all members of a tenant with their usernames.
func (s *Store) ListMembersByTenant(ctx context.Context, tenantID string) ([]Membership, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT m.tenant_id, t.name, m.user_id, u.username, m.role, m.created_at,
		        CASE WHEN t.created_by = m.user_id THEN 1 ELSE 0 END AS is_owner
		 FROM tenant_members m
		 JOIN tenants t ON t.id = m.tenant_id
		 JOIN users u ON u.id = m.user_id
		 WHERE m.tenant_id = ?
		 ORDER BY is_owner DESC, m.created_at ASC`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("query tenant members: %w", err)
	}
	defer rows.Close()

	var members []Membership
	for rows.Next() {
		var (
			m         Membership
			createdAt string
			isOwner   int
		)
		if err := rows.Scan(&m.TenantID, &m.TenantName, &m.UserID, &m.Username,
			&m.Role, &createdAt, &isOwner); err != nil {
			return nil, fmt.Errorf("scan tenant member: %w", err)
		}
		m.CreatedAt = parseUTC(createdAt)
		m.IsOwner = isOwner == 1
		members = append(members, m)
	}
	return members, rows.Err()
}

// AddMembership links an existing user to a tenant with the given role.
func (s *Store) AddMembership(ctx context.Context, tenantID, userID, role string) (*Membership, error) {
	now := time.Now().UTC()
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO tenant_members (id, tenant_id, user_id, role, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		newID(), tenantID, userID, role, formatUTC(now))
	if err != nil {
		if isUniqueViolation(err) {
			return nil, ErrMembershipExists
		}
		return nil, fmt.Errorf("insert membership: %w", err)
	}
	return s.GetMembership(ctx, tenantID, userID)
}

// GetTenant loads a tenant by ID.
func (s *Store) GetTenant(ctx context.Context, tenantID string) (*Tenant, error) {
	var (
		t         Tenant
		createdAt string
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT id, name, created_by, created_at FROM tenants WHERE id = ?`, tenantID,
	).Scan(&t.ID, &t.Name, &t.CreatedBy, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrMembershipNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan tenant: %w", err)
	}
	t.CreatedAt = parseUTC(createdAt)
	return &t, nil
}

// ---------------------------------------------------------------------------
// Refresh tokens
// ---------------------------------------------------------------------------

// CreateRefreshToken persists a hashed refresh token bound to (user, tenant).
func (s *Store) CreateRefreshToken(
	ctx context.Context, userID, tenantID, tokenHash string, expiresAt time.Time,
) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO refresh_tokens (id, user_id, tenant_id, token_hash, expires_at, revoked_at, created_at)
		 VALUES (?, ?, ?, ?, ?, NULL, ?)`,
		newID(), userID, tenantID, tokenHash,
		formatUTC(expiresAt.UTC()), formatUTC(time.Now().UTC()))
	if err != nil {
		return fmt.Errorf("insert refresh token: %w", err)
	}
	return nil
}

// GetRefreshTokenByHash loads a refresh token record by its hash.
func (s *Store) GetRefreshTokenByHash(ctx context.Context, tokenHash string) (*RefreshToken, error) {
	var (
		rt        RefreshToken
		expiresAt string
		revokedAt sql.NullString
		createdAt string
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT id, user_id, tenant_id, token_hash, expires_at, revoked_at, created_at
		 FROM refresh_tokens WHERE token_hash = ?`, tokenHash,
	).Scan(&rt.ID, &rt.UserID, &rt.TenantID, &rt.TokenHash, &expiresAt, &revokedAt, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrTokenNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan refresh token: %w", err)
	}
	rt.ExpiresAt = parseUTC(expiresAt)
	rt.CreatedAt = parseUTC(createdAt)
	if revokedAt.Valid {
		t := parseUTC(revokedAt.String)
		rt.RevokedAt = &t
	}
	return &rt, nil
}

// RevokeRefreshToken marks a single token as revoked (used during rotation
// and on logout).
func (s *Store) RevokeRefreshToken(ctx context.Context, tokenHash string) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE refresh_tokens SET revoked_at = ?
		 WHERE token_hash = ? AND revoked_at IS NULL`,
		formatUTC(time.Now().UTC()), tokenHash)
	if err != nil {
		return fmt.Errorf("revoke refresh token: %w", err)
	}
	return nil
}

// RevokeAllUserRefreshTokens invalidates every session of a user, e.g. after
// a password change.
func (s *Store) RevokeAllUserRefreshTokens(ctx context.Context, userID string) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`UPDATE refresh_tokens SET revoked_at = ?
		 WHERE user_id = ? AND revoked_at IS NULL`,
		formatUTC(time.Now().UTC()), userID)
	if err != nil {
		return 0, fmt.Errorf("revoke user refresh tokens: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// DeleteExpiredRefreshTokens purges tokens that can no longer be used.
func (s *Store) DeleteExpiredRefreshTokens(ctx context.Context) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM refresh_tokens WHERE expires_at < ?`, formatUTC(time.Now().UTC()))
	if err != nil {
		return 0, fmt.Errorf("delete expired refresh tokens: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}
