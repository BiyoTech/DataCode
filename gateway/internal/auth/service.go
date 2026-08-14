package auth

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
)

// Business-level errors mapped to HTTP status codes by the handler layer.
var (
	ErrInvalidCredentials = errors.New("invalid username or password")
	ErrAccountDisabled    = errors.New("account is disabled")
	ErrNoMembership       = errors.New("user has no tenant membership")
	ErrTooManyAttempts    = errors.New("too many failed login attempts")
	ErrWeakPassword       = errors.New("password does not meet complexity requirements")
	ErrInvalidRole        = errors.New("invalid role")
	ErrValidation         = errors.New("validation failed")
	ErrForbidden          = errors.New("operation not permitted")
)

// minPasswordLength is the minimum accepted password length.
const minPasswordLength = 8

// Identity is the resolved principal for one tenant context.
type Identity struct {
	UserID     string `json:"userId"`
	Username   string `json:"username"`
	TenantID   string `json:"tenantId"`
	TenantName string `json:"tenantName"`
	Role       string `json:"role"`
}

// Tokens is the credential pair handed to the client.
type Tokens struct {
	AccessToken  string `json:"accessToken"`
	RefreshToken string `json:"refreshToken"`
	// ExpiresAt is the access token expiry as a UTC RFC3339 string; the
	// frontend converts it to the user's timezone for display.
	ExpiresAt string `json:"expiresAt"`
}

// AuthResult is a completed authentication: an identity plus its tokens.
type AuthResult struct {
	User   Identity `json:"user"`
	Tokens Tokens   `json:"tokens"`
}

// MembershipInfo describes one selectable identity in the login picker.
type MembershipInfo struct {
	TenantID   string `json:"tenantId"`
	TenantName string `json:"tenantName"`
	Role       string `json:"role"`
	// IsOwner marks a tenant created by this user (primary account).
	IsOwner bool `json:"isOwner"`
	// IsLastUsed marks the identity used during the previous login.
	IsLastUsed bool `json:"isLastUsed"`
	// Username of the tenant owner, shown on sub-account cards.
	OwnerUsername string `json:"ownerUsername"`
}

// LoginResult is the two-phase login response: either a finished
// authentication or a request to choose one of several tenant identities.
type LoginResult struct {
	RequiresTenantSelection bool             `json:"requiresTenantSelection"`
	PreAuthToken            string           `json:"preAuthToken,omitempty"`
	Memberships             []MembershipInfo `json:"memberships,omitempty"`
	Auth                    *AuthResult      `json:"auth,omitempty"`
}

// MemberInfo describes a member of a tenant for the admin member list.
type MemberInfo struct {
	UserID   string `json:"userId"`
	Username string `json:"username"`
	Role     string `json:"role"`
	IsOwner  bool   `json:"isOwner"`
	// JoinedAt is UTC RFC3339; the frontend renders it in local time.
	JoinedAt string `json:"joinedAt"`
}

// Service holds the authentication use cases.
type Service struct {
	store      *Store
	issuer     *TokenIssuer
	limiter    *RateLimiter
	logger     *zap.Logger
	bcryptCost int

	// userLocks serializes token rotation and identity switching per user so
	// concurrent refreshes cannot race each other.
	userLocks sync.Map // map[string]*sync.Mutex
}

// NewService wires the authentication service.
func NewService(
	store *Store, issuer *TokenIssuer, limiter *RateLimiter,
	bcryptCost int, logger *zap.Logger,
) *Service {
	if bcryptCost < bcrypt.MinCost || bcryptCost > bcrypt.MaxCost {
		bcryptCost = bcrypt.DefaultCost
	}
	return &Service{
		store:      store,
		issuer:     issuer,
		limiter:    limiter,
		logger:     logger,
		bcryptCost: bcryptCost,
	}
}

// Issuer exposes the token issuer for middleware verification.
func (s *Service) Issuer() *TokenIssuer { return s.issuer }

// lockUser returns the per-user mutex used to serialize sensitive flows.
func (s *Service) lockUser(userID string) *sync.Mutex {
	value, _ := s.userLocks.LoadOrStore(userID, &sync.Mutex{})
	return value.(*sync.Mutex)
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

// Register creates a new identity together with its own tenant, making the
// registrant that tenant's admin, and immediately authenticates them.
func (s *Service) Register(ctx context.Context, username, password, tenantName string) (*AuthResult, error) {
	username = normalizeUsername(username)
	tenantName = strings.TrimSpace(tenantName)

	if err := validateUsername(username); err != nil {
		return nil, err
	}
	if tenantName == "" {
		return nil, fmt.Errorf("%w: organization name is required", ErrValidation)
	}
	if err := validatePassword(password); err != nil {
		return nil, err
	}

	hash, err := s.hashPassword(password)
	if err != nil {
		return nil, err
	}

	user, membership, err := s.store.RegisterTenant(ctx, username, hash, tenantName)
	if err != nil {
		return nil, err
	}

	s.logger.Info("auth_register_success",
		zap.String("user_id", user.ID),
		zap.String("username", user.Username),
		zap.String("tenant_id", membership.TenantID))

	return s.issueAuthResult(ctx, user, membership)
}

// ---------------------------------------------------------------------------
// Login / identity selection
// ---------------------------------------------------------------------------

// Login verifies credentials and either authenticates directly (single
// membership) or returns the identities to choose from.
func (s *Service) Login(ctx context.Context, username, password, clientIP string) (*LoginResult, error) {
	username = normalizeUsername(username)
	limiterKey := username + "|" + clientIP

	if allowed, retryAfter := s.limiter.Allowed(limiterKey); !allowed {
		s.logger.Warn("auth_login_rate_limited",
			zap.String("username", username),
			zap.Duration("retry_after", retryAfter))
		return nil, ErrTooManyAttempts
	}

	user, err := s.store.GetUserByUsername(ctx, username)
	if err != nil {
		if errors.Is(err, ErrUserNotFound) {
			s.limiter.RecordFailure(limiterKey)
			s.logger.Warn("auth_login_failure",
				zap.String("username", username), zap.String("reason", "user_not_found"))
			return nil, ErrInvalidCredentials
		}
		return nil, err
	}
	if user.Status != "active" {
		s.logger.Warn("auth_login_failure",
			zap.String("user_id", user.ID), zap.String("reason", "account_disabled"))
		return nil, ErrAccountDisabled
	}
	if bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(password)) != nil {
		s.limiter.RecordFailure(limiterKey)
		s.logger.Warn("auth_login_failure",
			zap.String("user_id", user.ID), zap.String("reason", "password_mismatch"))
		return nil, ErrInvalidCredentials
	}
	s.limiter.Reset(limiterKey)

	memberships, err := s.store.ListMembershipsByUser(ctx, user.ID)
	if err != nil {
		return nil, err
	}
	if len(memberships) == 0 {
		s.logger.Error("auth_login_no_membership", zap.String("user_id", user.ID))
		return nil, ErrNoMembership
	}

	// Single identity: skip the picker entirely.
	if len(memberships) == 1 {
		auth, err := s.issueAuthResult(ctx, user, &memberships[0])
		if err != nil {
			return nil, err
		}
		s.logger.Info("auth_login_success",
			zap.String("user_id", user.ID),
			zap.String("tenant_id", memberships[0].TenantID))
		return &LoginResult{Auth: auth}, nil
	}

	preAuthToken, _, err := s.issuer.IssuePreAuthToken(user.ID, user.Username)
	if err != nil {
		return nil, err
	}
	infos, err := s.toMembershipInfos(ctx, memberships, user.LastTenantID)
	if err != nil {
		return nil, err
	}

	s.logger.Info("auth_login_requires_tenant_selection",
		zap.String("user_id", user.ID),
		zap.Int("membership_count", len(memberships)))

	return &LoginResult{
		RequiresTenantSelection: true,
		PreAuthToken:            preAuthToken,
		Memberships:             infos,
	}, nil
}

// SelectTenant completes authentication for the chosen tenant identity. It is
// used both after login (pre-auth token) and for in-app identity switching
// (valid access token).
func (s *Service) SelectTenant(ctx context.Context, userID, tenantID string) (*AuthResult, error) {
	lock := s.lockUser(userID)
	lock.Lock()
	defer lock.Unlock()

	user, err := s.store.GetUserByID(ctx, userID)
	if err != nil {
		return nil, err
	}
	if user.Status != "active" {
		return nil, ErrAccountDisabled
	}

	membership, err := s.store.GetMembership(ctx, tenantID, userID)
	if err != nil {
		if errors.Is(err, ErrMembershipNotFound) {
			s.logger.Warn("auth_tenant_selection_denied",
				zap.String("user_id", userID), zap.String("tenant_id", tenantID))
			return nil, ErrForbidden
		}
		return nil, err
	}

	result, err := s.issueAuthResult(ctx, user, membership)
	if err != nil {
		return nil, err
	}

	s.logger.Info("auth_tenant_selected",
		zap.String("user_id", userID),
		zap.String("tenant_id", tenantID),
		zap.String("role", membership.Role))
	return result, nil
}

// ListMemberships returns every tenant identity available to a user.
func (s *Service) ListMemberships(ctx context.Context, userID string) ([]MembershipInfo, error) {
	user, err := s.store.GetUserByID(ctx, userID)
	if err != nil {
		return nil, err
	}
	memberships, err := s.store.ListMembershipsByUser(ctx, userID)
	if err != nil {
		return nil, err
	}
	return s.toMembershipInfos(ctx, memberships, user.LastTenantID)
}

// ---------------------------------------------------------------------------
// Token lifecycle
// ---------------------------------------------------------------------------

// Refresh rotates a refresh token, keeping the same (user, tenant) context.
func (s *Service) Refresh(ctx context.Context, rawToken string) (*AuthResult, error) {
	if strings.TrimSpace(rawToken) == "" {
		return nil, ErrInvalidToken
	}
	hash := HashRefreshToken(rawToken)

	record, err := s.store.GetRefreshTokenByHash(ctx, hash)
	if err != nil {
		if errors.Is(err, ErrTokenNotFound) {
			return nil, ErrInvalidToken
		}
		return nil, err
	}

	lock := s.lockUser(record.UserID)
	lock.Lock()
	defer lock.Unlock()

	if record.RevokedAt != nil {
		// A revoked token being replayed suggests theft: drop all sessions.
		s.logger.Warn("auth_refresh_reuse_detected",
			zap.String("user_id", record.UserID))
		if _, err := s.store.RevokeAllUserRefreshTokens(ctx, record.UserID); err != nil {
			s.logger.Error("auth_revoke_all_failed", zap.Error(err))
		}
		return nil, ErrInvalidToken
	}
	if record.ExpiresAt.Before(time.Now().UTC()) {
		return nil, ErrTokenExpired
	}

	user, err := s.store.GetUserByID(ctx, record.UserID)
	if err != nil {
		return nil, err
	}
	if user.Status != "active" {
		return nil, ErrAccountDisabled
	}
	membership, err := s.store.GetMembership(ctx, record.TenantID, record.UserID)
	if err != nil {
		// Membership revoked while the session was alive.
		return nil, ErrForbidden
	}

	// Rotate: the presented token is invalidated before a new pair is issued.
	if err := s.store.RevokeRefreshToken(ctx, hash); err != nil {
		return nil, err
	}
	result, err := s.issueAuthResult(ctx, user, membership)
	if err != nil {
		return nil, err
	}

	s.logger.Info("auth_token_refreshed",
		zap.String("user_id", user.ID), zap.String("tenant_id", membership.TenantID))
	return result, nil
}

// Logout revokes the presented refresh token.
func (s *Service) Logout(ctx context.Context, rawToken string) error {
	if strings.TrimSpace(rawToken) == "" {
		return nil
	}
	hash := HashRefreshToken(rawToken)
	record, err := s.store.GetRefreshTokenByHash(ctx, hash)
	if err != nil {
		// Unknown tokens are treated as already invalid.
		if errors.Is(err, ErrTokenNotFound) {
			return nil
		}
		return err
	}
	if err := s.store.RevokeRefreshToken(ctx, hash); err != nil {
		return err
	}
	s.logger.Info("auth_token_revoked",
		zap.String("user_id", record.UserID), zap.String("reason", "logout"))
	return nil
}

// ChangePassword validates the current password, stores the new hash and
// revokes every session of the user.
func (s *Service) ChangePassword(ctx context.Context, userID, currentPassword, newPassword string) error {
	if err := validatePassword(newPassword); err != nil {
		return err
	}
	user, err := s.store.GetUserByID(ctx, userID)
	if err != nil {
		return err
	}
	if bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(currentPassword)) != nil {
		s.logger.Warn("auth_change_password_denied", zap.String("user_id", userID))
		return ErrInvalidCredentials
	}
	if currentPassword == newPassword {
		return fmt.Errorf("%w: new password must differ from the current one", ErrValidation)
	}

	hash, err := s.hashPassword(newPassword)
	if err != nil {
		return err
	}
	if err := s.store.UpdatePasswordHash(ctx, userID, hash); err != nil {
		return err
	}
	revoked, err := s.store.RevokeAllUserRefreshTokens(ctx, userID)
	if err != nil {
		return err
	}

	s.logger.Info("auth_password_changed",
		zap.String("user_id", userID), zap.Int64("revoked_sessions", revoked))
	return nil
}

// ---------------------------------------------------------------------------
// Tenant member management (admin only, scoped to the acting tenant)
// ---------------------------------------------------------------------------

// ListMembers returns all members of a tenant.
func (s *Service) ListMembers(ctx context.Context, tenantID string) ([]MemberInfo, error) {
	memberships, err := s.store.ListMembersByTenant(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	members := make([]MemberInfo, 0, len(memberships))
	for _, m := range memberships {
		members = append(members, MemberInfo{
			UserID:   m.UserID,
			Username: m.Username,
			Role:     m.Role,
			IsOwner:  m.IsOwner,
			JoinedAt: m.CreatedAt.Format(time.RFC3339),
		})
	}
	return members, nil
}

// HasMember reports whether a user belongs to a tenant. It is a single indexed
// lookup, used by workspace access grants to verify the target account.
func (s *Service) HasMember(ctx context.Context, tenantID, userID string) (bool, error) {
	if tenantID == "" || userID == "" {
		return false, nil
	}
	if _, err := s.store.GetMembership(ctx, tenantID, userID); err != nil {
		if errors.Is(err, ErrMembershipNotFound) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// AddMemberResult reports whether a brand-new identity was created or an
// existing account was linked to the tenant.
type AddMemberResult struct {
	Member      MemberInfo `json:"member"`
	UserCreated bool       `json:"userCreated"`
}

// AddMember adds a sub account to a tenant. An existing username is linked
// as-is (its password is never touched); an unknown username creates a new
// identity with the supplied initial password.
//
// The role string is validated against the tenant's roles by the caller (the
// RBAC domain owns role validity), so only its presence is checked here.
func (s *Service) AddMember(
	ctx context.Context, tenantID, username, initialPassword, role string,
) (*AddMemberResult, error) {
	username = normalizeUsername(username)
	if err := validateUsername(username); err != nil {
		return nil, err
	}
	if strings.TrimSpace(role) == "" {
		return nil, ErrInvalidRole
	}

	created := false
	user, err := s.store.GetUserByUsername(ctx, username)
	switch {
	case errors.Is(err, ErrUserNotFound):
		if err := validatePassword(initialPassword); err != nil {
			return nil, err
		}
		hash, hashErr := s.hashPassword(initialPassword)
		if hashErr != nil {
			return nil, hashErr
		}
		user, err = s.store.CreateUser(ctx, username, hash)
		if err != nil {
			return nil, err
		}
		created = true
	case err != nil:
		return nil, err
	}

	membership, err := s.store.AddMembership(ctx, tenantID, user.ID, role)
	if err != nil {
		return nil, err
	}

	s.logger.Info("auth_member_added",
		zap.String("tenant_id", tenantID),
		zap.String("user_id", user.ID),
		zap.String("role", role),
		zap.Bool("user_created", created))

	return &AddMemberResult{
		Member: MemberInfo{
			UserID:   user.ID,
			Username: user.Username,
			Role:     membership.Role,
			IsOwner:  membership.IsOwner,
			JoinedAt: membership.CreatedAt.Format(time.RFC3339),
		},
		UserCreated: created,
	}, nil
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

// issueAuthResult mints an access/refresh pair for a resolved identity and
// records it as the user's most recent tenant.
func (s *Service) issueAuthResult(ctx context.Context, user *User, membership *Membership) (*AuthResult, error) {
	accessToken, expiresAt, err := s.issuer.IssueAccessToken(
		user.ID, user.Username, membership.TenantID, membership.Role)
	if err != nil {
		return nil, err
	}
	refreshToken, err := NewRefreshToken()
	if err != nil {
		return nil, err
	}
	refreshExpiry := time.Now().UTC().Add(s.issuer.RefreshTTL())
	if err := s.store.CreateRefreshToken(
		ctx, user.ID, membership.TenantID, HashRefreshToken(refreshToken), refreshExpiry,
	); err != nil {
		return nil, err
	}
	if err := s.store.SetLastTenant(ctx, user.ID, membership.TenantID); err != nil {
		// Non-fatal: only affects the "last used" hint in the picker.
		s.logger.Warn("auth_set_last_tenant_failed", zap.Error(err))
	}

	return &AuthResult{
		User: Identity{
			UserID:     user.ID,
			Username:   user.Username,
			TenantID:   membership.TenantID,
			TenantName: membership.TenantName,
			Role:       membership.Role,
		},
		Tokens: Tokens{
			AccessToken:  accessToken,
			RefreshToken: refreshToken,
			ExpiresAt:    expiresAt.Format(time.RFC3339),
		},
	}, nil
}

// toMembershipInfos enriches memberships with picker metadata (owner name and
// last-used flag).
func (s *Service) toMembershipInfos(
	ctx context.Context, memberships []Membership, lastTenantID string,
) ([]MembershipInfo, error) {
	infos := make([]MembershipInfo, 0, len(memberships))
	for _, m := range memberships {
		info := MembershipInfo{
			TenantID:   m.TenantID,
			TenantName: m.TenantName,
			Role:       m.Role,
			IsOwner:    m.IsOwner,
			IsLastUsed: m.TenantID == lastTenantID,
		}
		if !m.IsOwner {
			// Sub-account cards show which account owns the tenant.
			if tenant, err := s.store.GetTenant(ctx, m.TenantID); err == nil {
				if owner, err := s.store.GetUserByID(ctx, tenant.CreatedBy); err == nil {
					info.OwnerUsername = owner.Username
				}
			}
		}
		infos = append(infos, info)
	}
	return infos, nil
}

func (s *Service) hashPassword(password string) (string, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), s.bcryptCost)
	if err != nil {
		return "", fmt.Errorf("hash password: %w", err)
	}
	return string(hash), nil
}

// normalizeUsername lowercases and trims a username so lookups are stable.
func normalizeUsername(username string) string {
	return strings.ToLower(strings.TrimSpace(username))
}

func validateUsername(username string) error {
	if len(username) < 3 || len(username) > 64 {
		return fmt.Errorf("%w: username must be 3-64 characters", ErrValidation)
	}
	for _, r := range username {
		isAllowed := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') ||
			r == '.' || r == '_' || r == '-'
		if !isAllowed {
			return fmt.Errorf(
				"%w: username may only contain letters, digits, dot, underscore and hyphen",
				ErrValidation)
		}
	}
	return nil
}

func validatePassword(password string) error {
	if len(password) < minPasswordLength {
		return fmt.Errorf("%w: password must be at least %d characters",
			ErrWeakPassword, minPasswordLength)
	}
	var hasLetter, hasDigit bool
	for _, r := range password {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z'):
			hasLetter = true
		case r >= '0' && r <= '9':
			hasDigit = true
		}
	}
	if !hasLetter || !hasDigit {
		return fmt.Errorf("%w: password must contain letters and digits", ErrWeakPassword)
	}
	return nil
}
