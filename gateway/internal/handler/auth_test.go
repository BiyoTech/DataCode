package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"

	"github.com/agenticdata/gateway/internal/auth"
	"github.com/agenticdata/gateway/internal/middleware"
	"github.com/agenticdata/gateway/internal/rbac"
)

// newTestAuthHandler builds a handler backed by a temporary database.
func newTestAuthHandler(t *testing.T) (*AuthHandler, *auth.Service) {
	t.Helper()

	store, err := auth.NewStore(auth.DialectSQLite, filepath.Join(t.TempDir(), "handler-test.db"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })

	issuer, err := auth.NewTokenIssuer("handler-test-secret", time.Hour, 5*time.Minute, time.Hour)
	if err != nil {
		t.Fatalf("NewTokenIssuer: %v", err)
	}
	service := auth.NewService(store, issuer, auth.NewRateLimiter(5, time.Minute),
		bcrypt.MinCost, zap.NewNop())

	// Authorization data must exist before role validation can succeed.
	rbacService := rbac.NewService(rbac.NewStore(store.DB()), zap.NewNop())
	if err := rbacService.Bootstrap(context.Background()); err != nil {
		t.Fatalf("rbac Bootstrap: %v", err)
	}
	return NewAuthHandler(service, rbacService, zap.NewNop()), service
}

// postJSON issues a JSON POST request against a handler function.
func postJSON(
	t *testing.T, handlerFunc http.HandlerFunc, path string,
	payload interface{}, decorate func(*http.Request),
) *httptest.ResponseRecorder {
	t.Helper()

	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if decorate != nil {
		decorate(req)
	}
	rec := httptest.NewRecorder()
	handlerFunc(rec, req)
	return rec
}

// withIdentity injects a verified identity into the request context, mimicking
// what JWTAuth does for authenticated routes.
func withIdentity(r *http.Request, userID, tenantID, role string) *http.Request {
	ctx := r.Context()
	ctx = context.WithValue(ctx, middleware.UserIDKey, userID)
	ctx = context.WithValue(ctx, middleware.TenantIDKey, tenantID)
	ctx = context.WithValue(ctx, middleware.RoleKey, role)
	return r.WithContext(ctx)
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder, target interface{}) {
	t.Helper()
	if err := json.Unmarshal(rec.Body.Bytes(), target); err != nil {
		t.Fatalf("decode response: %v (body=%s)", err, rec.Body.String())
	}
}

func TestRegisterEndpoint(t *testing.T) {
	handler, _ := newTestAuthHandler(t)

	rec := postJSON(t, handler.Register, "/api/v1/auth/register", map[string]string{
		"username":   "founder",
		"password":   "secret123",
		"tenantName": "Acme Data",
	}, nil)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201 (body=%s)", rec.Code, rec.Body.String())
	}
	var result auth.AuthResult
	decodeBody(t, rec, &result)
	if result.User.Role != auth.RoleAdmin {
		t.Errorf("role = %q, want %q", result.User.Role, auth.RoleAdmin)
	}
	if result.Tokens.AccessToken == "" {
		t.Error("expected an access token")
	}

	// Duplicate usernames conflict.
	rec = postJSON(t, handler.Register, "/api/v1/auth/register", map[string]string{
		"username": "founder", "password": "secret123", "tenantName": "Other",
	}, nil)
	if rec.Code != http.StatusConflict {
		t.Errorf("duplicate status = %d, want 409", rec.Code)
	}
}

func TestRegisterEndpointRejectsMalformedBody(t *testing.T) {
	handler, _ := newTestAuthHandler(t)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/register",
		bytes.NewReader([]byte("{not json")))
	rec := httptest.NewRecorder()
	handler.Register(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	var body map[string]string
	decodeBody(t, rec, &body)
	if body["code"] != "invalid_body" {
		t.Errorf("code = %q, want %q", body["code"], "invalid_body")
	}
}

func TestLoginEndpointErrors(t *testing.T) {
	handler, _ := newTestAuthHandler(t)

	postJSON(t, handler.Register, "/api/v1/auth/register", map[string]string{
		"username": "member", "password": "secret123", "tenantName": "Org",
	}, nil)

	// Wrong password.
	rec := postJSON(t, handler.Login, "/api/v1/auth/login", map[string]string{
		"username": "member", "password": "wrong-password",
	}, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	var body map[string]string
	decodeBody(t, rec, &body)
	if body["code"] != "invalid_credentials" {
		t.Errorf("code = %q, want %q", body["code"], "invalid_credentials")
	}

	// Correct password.
	rec = postJSON(t, handler.Login, "/api/v1/auth/login", map[string]string{
		"username": "member", "password": "secret123",
	}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

func TestSelectTenantEndpointFlows(t *testing.T) {
	handler, service := newTestAuthHandler(t)
	ctx := context.Background()

	owner, err := service.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	if _, err := service.Register(ctx, "dual", "secret123", "Dual Org"); err != nil {
		t.Fatalf("Register dual: %v", err)
	}
	if _, err := service.AddMember(ctx, owner.User.TenantID, "dual", "", auth.RoleDeveloper); err != nil {
		t.Fatalf("AddMember: %v", err)
	}

	// Phase one returns a pre-auth token and the selectable identities.
	rec := postJSON(t, handler.Login, "/api/v1/auth/login", map[string]string{
		"username": "dual", "password": "secret123",
	}, nil)
	var login auth.LoginResult
	decodeBody(t, rec, &login)
	if !login.RequiresTenantSelection || login.PreAuthToken == "" {
		t.Fatalf("expected tenant selection, got %+v", login)
	}

	// tenantId is mandatory.
	rec = postJSON(t, handler.SelectTenant, "/api/v1/auth/select-tenant", map[string]string{
		"preAuthToken": login.PreAuthToken,
	}, nil)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("missing tenantId status = %d, want 400", rec.Code)
	}

	// Without any credential the request is unauthorized.
	rec = postJSON(t, handler.SelectTenant, "/api/v1/auth/select-tenant", map[string]string{
		"tenantId": owner.User.TenantID,
	}, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("anonymous status = %d, want 401", rec.Code)
	}

	// Phase two with the pre-auth token succeeds.
	rec = postJSON(t, handler.SelectTenant, "/api/v1/auth/select-tenant", map[string]string{
		"preAuthToken": login.PreAuthToken,
		"tenantId":     owner.User.TenantID,
	}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
	var selected auth.AuthResult
	decodeBody(t, rec, &selected)
	if selected.User.Role != auth.RoleDeveloper {
		t.Errorf("role = %q, want %q", selected.User.Role, auth.RoleDeveloper)
	}

	// In-app switching works with a valid access token instead.
	rec = postJSON(t, handler.SelectTenant, "/api/v1/auth/select-tenant", map[string]string{
		"tenantId": owner.User.TenantID,
	}, func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer "+selected.Tokens.AccessToken)
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("switch status = %d, want 200", rec.Code)
	}

	// An unrelated user cannot assume the owner's identity.
	outsider, err := service.Register(ctx, "outsider", "secret123", "Outsider Org")
	if err != nil {
		t.Fatalf("Register outsider: %v", err)
	}
	rec = postJSON(t, handler.SelectTenant, "/api/v1/auth/select-tenant", map[string]string{
		"tenantId": owner.User.TenantID,
	}, func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer "+outsider.Tokens.AccessToken)
	})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("outsider status = %d, want 403", rec.Code)
	}
}

func TestRefreshAndLogoutEndpoints(t *testing.T) {
	handler, service := newTestAuthHandler(t)

	registered, err := service.Register(context.Background(), "user", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}

	rec := postJSON(t, handler.Refresh, "/api/v1/auth/refresh", map[string]string{
		"refreshToken": registered.Tokens.RefreshToken,
	}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("refresh status = %d, want 200", rec.Code)
	}
	var refreshed auth.AuthResult
	decodeBody(t, rec, &refreshed)

	// The rotated token is invalid on replay.
	rec = postJSON(t, handler.Refresh, "/api/v1/auth/refresh", map[string]string{
		"refreshToken": registered.Tokens.RefreshToken,
	}, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("replay status = %d, want 401", rec.Code)
	}

	rec = postJSON(t, handler.Logout, "/api/v1/auth/logout", map[string]string{
		"refreshToken": refreshed.Tokens.RefreshToken,
	}, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("logout status = %d, want 200", rec.Code)
	}
	rec = postJSON(t, handler.Refresh, "/api/v1/auth/refresh", map[string]string{
		"refreshToken": refreshed.Tokens.RefreshToken,
	}, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("refresh after logout = %d, want 401", rec.Code)
	}
}

func TestChangePasswordEndpoint(t *testing.T) {
	handler, service := newTestAuthHandler(t)

	registered, err := service.Register(context.Background(), "changer", "secret123", "Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	identity := func(r *http.Request) {
		*r = *withIdentity(r, registered.User.UserID, registered.User.TenantID, auth.RoleAdmin)
	}

	rec := postJSON(t, handler.ChangePassword, "/api/v1/auth/change-password", map[string]string{
		"currentPassword": "wrong-password", "newPassword": "newsecret123",
	}, identity)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}

	rec = postJSON(t, handler.ChangePassword, "/api/v1/auth/change-password", map[string]string{
		"currentPassword": "secret123", "newPassword": "weak",
	}, identity)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("weak password status = %d, want 400", rec.Code)
	}

	rec = postJSON(t, handler.ChangePassword, "/api/v1/auth/change-password", map[string]string{
		"currentPassword": "secret123", "newPassword": "newsecret123",
	}, identity)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
}

func TestAddMemberEndpointIgnoresBodyTenant(t *testing.T) {
	handler, service := newTestAuthHandler(t)
	ctx := context.Background()

	owner, err := service.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	victim, err := service.Register(ctx, "victim", "secret123", "Victim Org")
	if err != nil {
		t.Fatalf("Register victim: %v", err)
	}

	// The body carries a foreign tenantId; the handler must scope to the token.
	rec := postJSON(t, handler.AddMember, "/api/v1/auth/members", map[string]string{
		"username": "newbie",
		"password": "initial123",
		"role":     auth.RoleViewer,
		"tenantId": victim.User.TenantID,
	}, func(r *http.Request) {
		*r = *withIdentity(r, owner.User.UserID, owner.User.TenantID, auth.RoleAdmin)
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201 (body=%s)", rec.Code, rec.Body.String())
	}

	// The victim's tenant is untouched.
	victimMembers, err := service.ListMembers(ctx, victim.User.TenantID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(victimMembers) != 1 {
		t.Fatalf("victim tenant members = %d, want 1", len(victimMembers))
	}

	ownerMembers, err := service.ListMembers(ctx, owner.User.TenantID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(ownerMembers) != 2 {
		t.Fatalf("owner tenant members = %d, want 2", len(ownerMembers))
	}
}

func TestListMembersEndpointScopedToToken(t *testing.T) {
	handler, service := newTestAuthHandler(t)
	ctx := context.Background()

	owner, err := service.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if _, err := service.AddMember(ctx, owner.User.TenantID, "helper", "initial123", auth.RoleDeveloper); err != nil {
		t.Fatalf("AddMember: %v", err)
	}

	req := withIdentity(httptest.NewRequest(http.MethodGet, "/api/v1/auth/members", nil),
		owner.User.UserID, owner.User.TenantID, auth.RoleAdmin)
	rec := httptest.NewRecorder()
	handler.ListMembers(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var payload struct {
		Members []auth.MemberInfo `json:"members"`
	}
	decodeBody(t, rec, &payload)
	if len(payload.Members) != 2 {
		t.Fatalf("members = %d, want 2", len(payload.Members))
	}
	// Timestamps are serialized as UTC for the frontend to localize.
	if _, err := time.Parse(time.RFC3339, payload.Members[0].JoinedAt); err != nil {
		t.Errorf("joinedAt is not RFC3339: %v", err)
	}
}

func TestCurrentUserEndpointReturnsVerifiedIdentity(t *testing.T) {
	handler, service := newTestAuthHandler(t)
	ctx := context.Background()

	owner, err := service.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}

	req := withIdentity(httptest.NewRequest(http.MethodGet, "/api/v1/me", nil),
		owner.User.UserID, owner.User.TenantID, auth.RoleAdmin)
	req = req.WithContext(context.WithValue(req.Context(), middleware.UsernameKey, "owner"))
	rec := httptest.NewRecorder()
	handler.CurrentUser(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body map[string]interface{}
	decodeBody(t, rec, &body)
	if body["username"] != "owner" {
		t.Errorf("username = %v, want owner", body["username"])
	}
	if body["tenantName"] != "Owner Org" {
		t.Errorf("tenantName = %v, want %q", body["tenantName"], "Owner Org")
	}
	if body["role"] != auth.RoleAdmin {
		t.Errorf("role = %v, want %q", body["role"], auth.RoleAdmin)
	}
	if count, ok := body["membershipCount"].(float64); !ok || int(count) != 1 {
		t.Errorf("membershipCount = %v, want 1", body["membershipCount"])
	}
}

func TestListMembershipsEndpoint(t *testing.T) {
	handler, service := newTestAuthHandler(t)
	ctx := context.Background()

	owner, err := service.Register(ctx, "owner", "secret123", "Owner Org")
	if err != nil {
		t.Fatalf("Register owner: %v", err)
	}
	dual, err := service.Register(ctx, "dual", "secret123", "Dual Org")
	if err != nil {
		t.Fatalf("Register dual: %v", err)
	}
	if _, err := service.AddMember(ctx, owner.User.TenantID, "dual", "", auth.RoleViewer); err != nil {
		t.Fatalf("AddMember: %v", err)
	}

	req := withIdentity(httptest.NewRequest(http.MethodGet, "/api/v1/auth/memberships", nil),
		dual.User.UserID, dual.User.TenantID, auth.RoleAdmin)
	rec := httptest.NewRecorder()
	handler.ListMemberships(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var payload struct {
		Memberships []auth.MembershipInfo `json:"memberships"`
	}
	decodeBody(t, rec, &payload)
	if len(payload.Memberships) != 2 {
		t.Fatalf("memberships = %d, want 2", len(payload.Memberships))
	}
	if !payload.Memberships[0].IsOwner {
		t.Error("owned tenant should come first")
	}
	if payload.Memberships[1].OwnerUsername != "owner" {
		t.Errorf("ownerUsername = %q, want %q",
			payload.Memberships[1].OwnerUsername, "owner")
	}
}
