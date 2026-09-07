package auth

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
)

// newTestService builds an isolated service backed by a temporary database.
func newTestService(t *testing.T) *Service {
	t.Helper()

	store, err := NewStore(DialectSQLite, filepath.Join(t.TempDir(), "auth-test.db"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })

	issuer, err := NewTokenIssuer("test-secret-value", time.Hour, 5*time.Minute, 24*time.Hour)
	if err != nil {
		t.Fatalf("NewTokenIssuer: %v", err)
	}
	return NewService(store, issuer, NewRateLimiter(3, time.Minute),
		bcrypt.MinCost, zap.NewNop())
}

func TestRegisterCreatesTenantAndAdminMembership(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	result, err := svc.Register(ctx, "Owner.One", "secret123", "Acme Analytics")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	// Usernames are normalized to lowercase for stable lookups.
	if result.User.Username != "owner.one" {
		t.Errorf("username = %q, want %q", result.User.Username, "owner.one")
	}
	if result.User.Role != RoleAdmin {
		t.Errorf("role = %q, want %q", result.User.Role, RoleAdmin)
	}
	if result.User.TenantName != "Acme Analytics" {
		t.Errorf("tenantName = %q, want %q", result.User.TenantName, "Acme Analytics")
	}
	if result.Tokens.AccessToken == "" || result.Tokens.RefreshToken == "" {
		t.Error("expected both tokens to be issued")
	}

	// The access token must carry the tenant context.
	claims, err := svc.Issuer().Verify(result.Tokens.AccessToken, TokenTypeAccess)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if claims.TenantID != result.User.TenantID {
		t.Errorf("claims tenant = %q, want %q", claims.TenantID, result.User.TenantID)
	}
}

func TestRegisterRejectsDuplicateUsernameAtomically(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	if _, err := svc.Register(ctx, "owner", "secret123", "First Org"); err != nil {
		t.Fatalf("first Register: %v", err)
	}
	if _, err := svc.Register(ctx, "owner", "secret123", "Second Org"); !errors.Is(err, ErrUsernameTaken) {
		t.Fatalf("second Register error = %v, want ErrUsernameTaken", err)
	}

	// The failed registration must not have left a dangling tenant behind.
	memberships, err := svc.ListMemberships(ctx, mustUserID(t, svc, "owner"))
	if err != nil {
		t.Fatalf("ListMemberships: %v", err)
	}
	if len(memberships) != 1 {
		t.Fatalf("memberships = %d, want 1", len(memberships))
	}
}

func TestRegisterValidatesInput(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	cases := []struct {
		name     string
		username string
		password string
		tenant   string
		wantErr  error
	}{
		{"short username", "ab", "secret123", "Org", ErrValidation},
		{"invalid characters", "bad user!", "secret123", "Org", ErrValidation},
		{"missing tenant name", "gooduser", "secret123", "  ", ErrValidation},
		{"short password", "gooduser", "abc1", "Org", ErrWeakPassword},
		{"letters only password", "gooduser", "abcdefghij", "Org", ErrWeakPassword},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := svc.Register(ctx, tc.username, tc.password, tc.tenant)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("error = %v, want %v", err, tc.wantErr)
			}
		})
	}
}

func TestLoginSingleMembershipSkipsSelection(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	if _, err := svc.Register(ctx, "solo", "secret123", "Solo Org"); err != nil {
		t.Fatalf("Register: %v", err)
	}

	result, err := svc.Login(ctx, "solo", "secret123", "127.0.0.1")
	if err != nil {
		t.Fatalf("Login: %v", err)
	}
	if result.RequiresTenantSelection {
		t.Error("single membership should not require selection")
	}
	if result.Auth == nil || result.Auth.Tokens.AccessToken == "" {
		t.Fatal("expected an authenticated result")
	}
}

func TestLoginMultipleMembershipsRequiresSelection(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	owner, err := svc.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	// "member" owns its own tenant and is invited into the owner's tenant,
	// i.e. it is both a primary and a sub account.
	member, err := svc.Register(ctx, "member", "secret123", "Member Org")
	if err != nil {
		t.Fatalf("Register member: %v", err)
	}
	if _, err := svc.AddMember(ctx, owner.User.TenantID, "member", "", RoleDeveloper); err != nil {
		t.Fatalf("AddMember: %v", err)
	}

	result, err := svc.Login(ctx, "member", "secret123", "127.0.0.1")
	if err != nil {
		t.Fatalf("Login: %v", err)
	}
	if !result.RequiresTenantSelection {
		t.Fatal("expected tenant selection to be required")
	}
	if result.Auth != nil {
		t.Error("tokens must not be issued before an identity is chosen")
	}
	if result.PreAuthToken == "" {
		t.Fatal("expected a pre-auth token")
	}
	if len(result.Memberships) != 2 {
		t.Fatalf("memberships = %d, want 2", len(result.Memberships))
	}

	// Owned tenant first, and it is flagged as the last used identity.
	if !result.Memberships[0].IsOwner {
		t.Error("owned tenant should be listed first")
	}
	if result.Memberships[0].TenantID != member.User.TenantID {
		t.Errorf("first tenant = %q, want %q",
			result.Memberships[0].TenantID, member.User.TenantID)
	}
	if !result.Memberships[0].IsLastUsed {
		t.Error("registration tenant should be marked as last used")
	}
	// The sub-account card exposes the owner of the foreign tenant.
	sub := result.Memberships[1]
	if sub.IsOwner {
		t.Error("invited membership must not be flagged as owner")
	}
	if sub.OwnerUsername != "owner" {
		t.Errorf("ownerUsername = %q, want %q", sub.OwnerUsername, "owner")
	}
	if sub.Role != RoleDeveloper {
		t.Errorf("role = %q, want %q", sub.Role, RoleDeveloper)
	}

	// A pre-auth token cannot be used as an access token.
	if _, err := svc.Issuer().Verify(result.PreAuthToken, TokenTypeAccess); !errors.Is(err, ErrWrongTokenType) {
		t.Fatalf("pre-auth token accepted as access token: %v", err)
	}
}

func TestLoginWrongPasswordAndRateLimit(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	if _, err := svc.Register(ctx, "victim", "secret123", "Org"); err != nil {
		t.Fatalf("Register: %v", err)
	}

	// The limiter is configured with maxFailures = 3.
	for i := 0; i < 3; i++ {
		if _, err := svc.Login(ctx, "victim", "wrong-password", "10.0.0.1"); !errors.Is(err, ErrInvalidCredentials) {
			t.Fatalf("attempt %d error = %v, want ErrInvalidCredentials", i, err)
		}
	}
	if _, err := svc.Login(ctx, "victim", "secret123", "10.0.0.1"); !errors.Is(err, ErrTooManyAttempts) {
		t.Fatalf("error = %v, want ErrTooManyAttempts", err)
	}
	// Lockout is scoped per client IP, so another address is unaffected.
	if _, err := svc.Login(ctx, "victim", "secret123", "10.0.0.2"); err != nil {
		t.Fatalf("login from a different IP failed: %v", err)
	}
}

func TestLoginUnknownUserYieldsGenericError(t *testing.T) {
	svc := newTestService(t)

	// Unknown users and wrong passwords must be indistinguishable.
	if _, err := svc.Login(context.Background(), "ghost", "secret123", "10.0.0.1"); !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("error = %v, want ErrInvalidCredentials", err)
	}
}

func TestSelectTenantRejectsForeignTenant(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	owner, err := svc.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	outsider, err := svc.Register(ctx, "outsider", "secret123", "Outsider Org")
	if err != nil {
		t.Fatalf("Register outsider: %v", err)
	}

	// A user without a membership cannot assume that tenant identity.
	if _, err := svc.SelectTenant(ctx, outsider.User.UserID, owner.User.TenantID); !errors.Is(err, ErrForbidden) {
		t.Fatalf("error = %v, want ErrForbidden", err)
	}

	// After being invited the same selection succeeds with the granted role.
	if _, err := svc.AddMember(ctx, owner.User.TenantID, "outsider", "", RoleViewer); err != nil {
		t.Fatalf("AddMember: %v", err)
	}
	result, err := svc.SelectTenant(ctx, outsider.User.UserID, owner.User.TenantID)
	if err != nil {
		t.Fatalf("SelectTenant: %v", err)
	}
	if result.User.Role != RoleViewer {
		t.Errorf("role = %q, want %q", result.User.Role, RoleViewer)
	}
	if result.User.TenantID != owner.User.TenantID {
		t.Errorf("tenant = %q, want %q", result.User.TenantID, owner.User.TenantID)
	}
}

func TestRefreshRotatesAndInvalidatesOldToken(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	registered, err := svc.Register(ctx, "rotator", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	first := registered.Tokens.RefreshToken

	rotated, err := svc.Refresh(ctx, first)
	if err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if rotated.Tokens.RefreshToken == first {
		t.Error("refresh token was not rotated")
	}
	if rotated.User.TenantID != registered.User.TenantID {
		t.Error("refresh must preserve the tenant context")
	}

	// Replaying the consumed token is rejected and, as theft mitigation,
	// invalidates every session of that user.
	if _, err := svc.Refresh(ctx, first); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("replay error = %v, want ErrInvalidToken", err)
	}
	if _, err := svc.Refresh(ctx, rotated.Tokens.RefreshToken); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("post-reuse refresh error = %v, want ErrInvalidToken", err)
	}
}

func TestLogoutRevokesRefreshToken(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	registered, err := svc.Register(ctx, "leaver", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if err := svc.Logout(ctx, registered.Tokens.RefreshToken); err != nil {
		t.Fatalf("Logout: %v", err)
	}
	if _, err := svc.Refresh(ctx, registered.Tokens.RefreshToken); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("error = %v, want ErrInvalidToken", err)
	}
	// Logging out with an unknown token is a no-op, not an error.
	if err := svc.Logout(ctx, "unknown-token"); err != nil {
		t.Fatalf("Logout with unknown token: %v", err)
	}
}

func TestChangePasswordRevokesAllSessions(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	registered, err := svc.Register(ctx, "changer", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	userID := registered.User.UserID

	if err := svc.ChangePassword(ctx, userID, "wrong-password", "newsecret123"); !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("error = %v, want ErrInvalidCredentials", err)
	}
	if err := svc.ChangePassword(ctx, userID, "secret123", "short"); !errors.Is(err, ErrWeakPassword) {
		t.Fatalf("error = %v, want ErrWeakPassword", err)
	}
	if err := svc.ChangePassword(ctx, userID, "secret123", "secret123"); !errors.Is(err, ErrValidation) {
		t.Fatalf("error = %v, want ErrValidation", err)
	}

	if err := svc.ChangePassword(ctx, userID, "secret123", "newsecret123"); err != nil {
		t.Fatalf("ChangePassword: %v", err)
	}
	// Existing sessions are dropped and only the new password works.
	if _, err := svc.Refresh(ctx, registered.Tokens.RefreshToken); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("stale session still valid: %v", err)
	}
	if _, err := svc.Login(ctx, "changer", "secret123", "127.0.0.1"); !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("old password still accepted: %v", err)
	}
	if _, err := svc.Login(ctx, "changer", "newsecret123", "127.0.0.1"); err != nil {
		t.Fatalf("login with new password: %v", err)
	}
}

func TestAddMemberLinksExistingAccountWithoutTouchingPassword(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	owner, err := svc.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	if _, err := svc.Register(ctx, "existing", "ownsecret123", "Existing Org"); err != nil {
		t.Fatalf("Register existing: %v", err)
	}

	result, err := svc.AddMember(ctx, owner.User.TenantID, "existing", "ignored-password1", RoleDeveloper)
	if err != nil {
		t.Fatalf("AddMember: %v", err)
	}
	if result.UserCreated {
		t.Error("an existing account must be linked, not recreated")
	}
	// The invited user keeps its own credentials.
	if _, err := svc.Login(ctx, "existing", "ownsecret123", "127.0.0.1"); err != nil {
		t.Fatalf("existing password no longer works: %v", err)
	}

	// Duplicate invitations are rejected.
	if _, err := svc.AddMember(ctx, owner.User.TenantID, "existing", "", RoleViewer); !errors.Is(err, ErrMembershipExists) {
		t.Fatalf("error = %v, want ErrMembershipExists", err)
	}
}

func TestAddMemberCreatesNewIdentity(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	owner, err := svc.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}

	// A new username requires a valid initial password.
	if _, err := svc.AddMember(ctx, owner.User.TenantID, "newbie", "weak", RoleViewer); !errors.Is(err, ErrWeakPassword) {
		t.Fatalf("error = %v, want ErrWeakPassword", err)
	}
	// Role validity is owned by the RBAC domain (roles live in the database and
	// are tenant-editable); this layer only rejects an empty role.
	if _, err := svc.AddMember(ctx, owner.User.TenantID, "newbie", "initial123", ""); !errors.Is(err, ErrInvalidRole) {
		t.Fatalf("error = %v, want ErrInvalidRole", err)
	}

	result, err := svc.AddMember(ctx, owner.User.TenantID, "newbie", "initial123", RoleViewer)
	if err != nil {
		t.Fatalf("AddMember: %v", err)
	}
	if !result.UserCreated {
		t.Error("expected a new identity to be created")
	}

	// The sub account can log in and lands directly in the inviting tenant.
	login, err := svc.Login(ctx, "newbie", "initial123", "127.0.0.1")
	if err != nil {
		t.Fatalf("Login: %v", err)
	}
	if login.RequiresTenantSelection {
		t.Error("a sub account with one membership should not need selection")
	}
	if login.Auth.User.TenantID != owner.User.TenantID {
		t.Errorf("tenant = %q, want %q", login.Auth.User.TenantID, owner.User.TenantID)
	}
}

func TestListMembersIsScopedToTenant(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	first, err := svc.Register(ctx, "first", "secret123", "First Org")
	if err != nil {
		t.Fatalf("Register first: %v", err)
	}
	second, err := svc.Register(ctx, "second", "secret123", "Second Org")
	if err != nil {
		t.Fatalf("Register second: %v", err)
	}
	if _, err := svc.AddMember(ctx, first.User.TenantID, "helper", "initial123", RoleDeveloper); err != nil {
		t.Fatalf("AddMember: %v", err)
	}

	firstMembers, err := svc.ListMembers(ctx, first.User.TenantID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(firstMembers) != 2 {
		t.Fatalf("first tenant members = %d, want 2", len(firstMembers))
	}
	if !firstMembers[0].IsOwner || firstMembers[0].Username != "first" {
		t.Error("tenant owner should be listed first")
	}

	// The other tenant only ever sees its own member.
	secondMembers, err := svc.ListMembers(ctx, second.User.TenantID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(secondMembers) != 1 || secondMembers[0].Username != "second" {
		t.Fatalf("second tenant members = %+v, want only 'second'", secondMembers)
	}
}

func TestRefreshFailsAfterMembershipRemoval(t *testing.T) {
	svc := newTestService(t)
	ctx := context.Background()

	registered, err := svc.Register(ctx, "gone", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}

	// Simulate the tenant identity disappearing underneath a live session.
	if _, err := svc.store.db.ExecContext(ctx,
		`DELETE FROM tenant_members WHERE user_id = ?`, registered.User.UserID); err != nil {
		t.Fatalf("delete membership: %v", err)
	}
	if _, err := svc.Refresh(ctx, registered.Tokens.RefreshToken); !errors.Is(err, ErrForbidden) {
		t.Fatalf("error = %v, want ErrForbidden", err)
	}
}

func TestExpiredRefreshTokenRejectedAndPurged(t *testing.T) {
	store, err := NewStore(DialectSQLite, filepath.Join(t.TempDir(), "auth-expiry.db"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })

	// A negative refresh TTL yields an already-expired token.
	issuer, err := NewTokenIssuer("test-secret-value", time.Hour, time.Minute, -time.Hour)
	if err != nil {
		t.Fatalf("NewTokenIssuer: %v", err)
	}
	svc := NewService(store, issuer, NewRateLimiter(5, time.Minute), bcrypt.MinCost, zap.NewNop())
	ctx := context.Background()

	registered, err := svc.Register(ctx, "stale", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if _, err := svc.Refresh(ctx, registered.Tokens.RefreshToken); !errors.Is(err, ErrTokenExpired) {
		t.Fatalf("error = %v, want ErrTokenExpired", err)
	}

	purged, err := store.DeleteExpiredRefreshTokens(ctx)
	if err != nil {
		t.Fatalf("DeleteExpiredRefreshTokens: %v", err)
	}
	if purged != 1 {
		t.Errorf("purged = %d, want 1", purged)
	}
}

func TestTokenIssuerRequiresSecret(t *testing.T) {
	if _, err := NewTokenIssuer("   ", time.Hour, time.Minute, time.Hour); !errors.Is(err, ErrMissingSecret) {
		t.Fatalf("error = %v, want ErrMissingSecret", err)
	}
}

func TestVerifyRejectsForeignSignature(t *testing.T) {
	issuer, _ := NewTokenIssuer("secret-one", time.Hour, time.Minute, time.Hour)
	other, _ := NewTokenIssuer("secret-two", time.Hour, time.Minute, time.Hour)

	token, _, err := issuer.IssueAccessToken("u1", "user", "t1", RoleAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}
	if _, err := other.Verify(token, TokenTypeAccess); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("error = %v, want ErrInvalidToken", err)
	}
}

func TestVerifyRejectsExpiredAccessToken(t *testing.T) {
	issuer, _ := NewTokenIssuer("secret", -time.Minute, time.Minute, time.Hour)

	token, _, err := issuer.IssueAccessToken("u1", "user", "t1", RoleAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}
	if _, err := issuer.Verify(token, TokenTypeAccess); !errors.Is(err, ErrTokenExpired) {
		t.Fatalf("error = %v, want ErrTokenExpired", err)
	}
}

func TestHashRefreshTokenIsDeterministicAndNotReversible(t *testing.T) {
	raw, err := NewRefreshToken()
	if err != nil {
		t.Fatalf("NewRefreshToken: %v", err)
	}
	if HashRefreshToken(raw) != HashRefreshToken(raw) {
		t.Error("hash must be deterministic")
	}
	if HashRefreshToken(raw) == raw {
		t.Error("stored hash must differ from the raw token")
	}

	other, _ := NewRefreshToken()
	if raw == other {
		t.Error("refresh tokens must be unique")
	}
}

func TestRateLimiterUnlocksAfterWindow(t *testing.T) {
	limiter := NewRateLimiter(2, 20*time.Millisecond)

	limiter.RecordFailure("key")
	if allowed, _ := limiter.Allowed("key"); !allowed {
		t.Fatal("one failure should not lock the key")
	}
	limiter.RecordFailure("key")
	if allowed, retryAfter := limiter.Allowed("key"); allowed || retryAfter <= 0 {
		t.Fatal("key should be locked after reaching the threshold")
	}

	time.Sleep(30 * time.Millisecond)
	if allowed, _ := limiter.Allowed("key"); !allowed {
		t.Fatal("lockout should elapse")
	}

	// A successful login clears the counters.
	limiter.RecordFailure("key")
	limiter.Reset("key")
	if allowed, _ := limiter.Allowed("key"); !allowed {
		t.Fatal("reset should clear the failure state")
	}
}

// mustUserID resolves a username to its ID, failing the test if absent.
func mustUserID(t *testing.T, svc *Service, username string) string {
	t.Helper()
	user, err := svc.store.GetUserByUsername(context.Background(), username)
	if err != nil {
		t.Fatalf("GetUserByUsername(%q): %v", username, err)
	}
	return user.ID
}
