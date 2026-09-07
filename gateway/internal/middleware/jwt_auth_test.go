package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
	"github.com/datacode/gateway/internal/authz"
	rbac "github.com/datacode/shared/rbac"
)

const testSecret = "middleware-test-secret"

// newTestIssuer builds an issuer with the given access token lifetime.
func newTestIssuer(t *testing.T, accessTTL time.Duration) *auth.TokenIssuer {
	t.Helper()
	issuer, err := auth.NewTokenIssuer(testSecret, accessTTL, 5*time.Minute, time.Hour)
	if err != nil {
		t.Fatalf("NewTokenIssuer: %v", err)
	}
	return issuer
}

// identityEcho reports the identity the middleware published, so tests can
// assert both context values and rewritten downstream headers.
func identityEcho(captured *map[string]string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*captured = map[string]string{
			"ctx_user_id":     GetUserID(r.Context()),
			"ctx_tenant_id":   GetTenantID(r.Context()),
			"ctx_username":    GetUsername(r.Context()),
			"ctx_role":        GetRole(r.Context()),
			"header_user_id":  r.Header.Get("X-User-ID"),
			"header_tenant":   r.Header.Get("X-Tenant-ID"),
			"header_username": r.Header.Get("X-Username"),
		}
		w.WriteHeader(http.StatusOK)
	})
}

func TestJWTAuthPublishesIdentityAndOverwritesHeaders(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)
	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	var captured map[string]string
	handler := JWTAuth(issuer, zap.NewNop())(identityEcho(&captured))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/sqlmesh/models", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	// A malicious client tries to impersonate another tenant identity.
	req.Header.Set("X-User-ID", "attacker")
	req.Header.Set("X-Tenant-ID", "victim-tenant")

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	want := map[string]string{
		"ctx_user_id":     "user-1",
		"ctx_tenant_id":   "tenant-1",
		"ctx_username":    "alice",
		"ctx_role":        rbac.RoleTenantAdmin,
		"header_user_id":  "user-1",
		"header_tenant":   "tenant-1",
		"header_username": "alice",
	}
	for key, expected := range want {
		if captured[key] != expected {
			t.Errorf("%s = %q, want %q", key, captured[key], expected)
		}
	}
}

func TestJWTAuthRejectsMissingAndMalformedTokens(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)
	handler := JWTAuth(issuer, zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Error("handler must not be reached")
		}))

	cases := []struct {
		name     string
		header   string
		wantCode string
	}{
		{"no header", "", "unauthorized"},
		{"wrong scheme", "Basic dXNlcjpwYXNz", "unauthorized"},
		{"garbage token", "Bearer not-a-jwt", "unauthorized"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
			if tc.header != "" {
				req.Header.Set("Authorization", tc.header)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("status = %d, want 401", rec.Code)
			}
			var body map[string]string
			if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
				t.Fatalf("decode body: %v", err)
			}
			if body["code"] != tc.wantCode {
				t.Errorf("code = %q, want %q", body["code"], tc.wantCode)
			}
		})
	}
}

func TestJWTAuthSignalsExpiredToken(t *testing.T) {
	// The frontend relies on this distinct code to trigger a silent refresh.
	issuer := newTestIssuer(t, -time.Minute)
	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantViewer)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	handler := JWTAuth(issuer, zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Error("handler must not be reached")
		}))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	var body map[string]string
	json.NewDecoder(rec.Body).Decode(&body)
	if body["code"] != "token_expired" {
		t.Errorf("code = %q, want %q", body["code"], "token_expired")
	}
}

func TestJWTAuthRejectsPreAuthToken(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)
	preAuth, _, err := issuer.IssuePreAuthToken("user-1", "alice")
	if err != nil {
		t.Fatalf("IssuePreAuthToken: %v", err)
	}

	handler := JWTAuth(issuer, zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Error("a pre-auth token must never reach business APIs")
		}))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/sqlmesh/models", nil)
	req.Header.Set("Authorization", "Bearer "+preAuth)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

func TestJWTAuthAllowsPublicPathsAndPreflight(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)

	reached := false
	handler := JWTAuth(issuer, zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			reached = true
			w.WriteHeader(http.StatusOK)
		}))

	publicRequests := []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/health"},
		{http.MethodPost, "/api/v1/auth/register"},
		{http.MethodPost, "/api/v1/auth/login"},
		{http.MethodPost, "/api/v1/auth/login/"}, // trailing slash normalized
		{http.MethodPost, "/api/v1/auth/select-tenant"},
		{http.MethodPost, "/api/v1/auth/refresh"},
		{http.MethodOptions, "/api/v1/sqlmesh/models"}, // CORS preflight
	}
	for _, tc := range publicRequests {
		reached = false
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
		if !reached {
			t.Errorf("%s %s was blocked but should be public", tc.method, tc.path)
		}
	}

	// Authenticated auth endpoints stay protected.
	protected := []string{"/api/v1/auth/logout", "/api/v1/auth/memberships", "/api/v1/auth/members"}
	for _, path := range protected {
		reached = false
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if reached {
			t.Errorf("%s must require authentication", path)
		}
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s status = %d, want 401", path, rec.Code)
		}
	}
}

// TestAuthorizeEnforcesTenantPermissions verifies that the route map is applied
// against the database-defined tenant roles: reading the member list is granted
// to every built-in role, while writing it is reserved to administrators.
func TestAuthorizeEnforcesTenantPermissions(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)

	cases := []struct {
		name       string
		role       string
		method     string
		wantStatus int
	}{
		{"admin reads members", rbac.RoleTenantAdmin, http.MethodGet, http.StatusOK},
		{"developer reads members", rbac.RoleTenantDeveloper, http.MethodGet, http.StatusOK},
		{"admin writes members", rbac.RoleTenantAdmin, http.MethodPost, http.StatusOK},
		{"developer writes members", rbac.RoleTenantDeveloper, http.MethodPost, http.StatusForbidden},
		{"viewer writes members", rbac.RoleTenantViewer, http.MethodPost, http.StatusForbidden},
		{"unknown role writes members", "ghost", http.MethodPost, http.StatusForbidden},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", tc.role)
			if err != nil {
				t.Fatalf("IssueAccessToken: %v", err)
			}
			terminal := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
			})
			resolver := newTestResolver(t, "tenant-1", "user-1", tc.role)
			handler := JWTAuth(issuer, zap.NewNop())(
				Authorize(resolver, zap.NewNop())(terminal))

			req := httptest.NewRequest(tc.method, "/api/v1/auth/members", nil)
			req.Header.Set("Authorization", "Bearer "+token)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if rec.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tc.wantStatus)
			}
		})
	}
}

// TestAuthorizeDeniesUnmappedWrites checks the default-deny fallback: a
// state-changing request that matches no rule must never reach the backend.
func TestAuthorizeDeniesUnmappedWrites(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)

	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	reached := false
	handler := JWTAuth(issuer, zap.NewNop())(
		Authorize(newTestResolver(t, "tenant-1", "user-1", rbac.RoleTenantAdmin),
			zap.NewNop())(
			http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				reached = true
				w.WriteHeader(http.StatusOK)
			})))

	req := httptest.NewRequest(http.MethodPost, "/api/v1/brand-new-write-endpoint", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if reached {
		t.Error("an unmapped write endpoint must not be reachable")
	}
	if rec.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", rec.Code)
	}
	assertDenial(t, rec, DenyRouteNotAllowed, "")
}

// TestAuthorizeDenialExplainsMissingPermission pins the 403 contract: every
// rejection must name a reason and, when applicable, the permission the caller
// lacks — otherwise the UI can only show "status code 403".
func TestAuthorizeDenialExplainsMissingPermission(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)

	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantViewer)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}
	resolver := newTestResolver(t, "tenant-1", "user-1", rbac.RoleTenantViewer)

	handler := JWTAuth(issuer, zap.NewNop())(
		Authorize(resolver, zap.NewNop())(
			http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
			})))

	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/members", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	assertDenial(t, rec, DenyMissingPermission, "tenant:member:write")
}

// assertDenial verifies the shape and content of a 403 payload.
func assertDenial(
	t *testing.T, rec *httptest.ResponseRecorder, reason DenyReason, permission string,
) {
	t.Helper()

	var body struct {
		Code               string `json:"code"`
		Reason             string `json:"reason"`
		Message            string `json:"message"`
		RequiredPermission string `json:"requiredPermission"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode denial body: %v (raw: %s)", err, rec.Body.String())
	}
	if body.Code != "forbidden" {
		t.Errorf("code = %q, want \"forbidden\"", body.Code)
	}
	if body.Reason != string(reason) {
		t.Errorf("reason = %q, want %q", body.Reason, reason)
	}
	if body.RequiredPermission != permission {
		t.Errorf("requiredPermission = %q, want %q", body.RequiredPermission, permission)
	}
	if body.Message == "" {
		t.Error("message must never be empty: it is what the user sees")
	}
	if permission != "" && !strings.Contains(body.Message, permission) {
		t.Errorf("message %q must name the missing permission %q", body.Message, permission)
	}
}

// TestAuthorizeStripsClientCapabilityHeaders makes sure a client cannot inject
// its own capabilities: the headers are always produced by the gateway.
func TestAuthorizeStripsClientCapabilityHeaders(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)

	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantViewer)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}
	resolver := newTestResolver(t, "tenant-1", "user-1", rbac.RoleTenantViewer)

	var forwarded string
	handler := JWTAuth(issuer, zap.NewNop())(
		Authorize(resolver, zap.NewNop())(
			http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				forwarded = r.Header.Get(HeaderPermissions)
				w.WriteHeader(http.StatusOK)
			})))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/auth/members", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set(HeaderPermissions, "tenant:member:write,workspace:delete")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if strings.Contains(forwarded, "tenant:member:write") {
		t.Errorf("forged capability survived: %q", forwarded)
	}
}

// builtinRolePermissions returns the permission set of a built-in role, taken
// from the code-side templates in internal/rbac.
//
// P5.5: these tests used to seed a throwaway SQLite database and ask a real
// rbac.Service. The gateway no longer owns those tables — identity-service
// does — so the reference is now the template declaration itself. The intent is
// unchanged: assert against the platform's ACTUAL role definitions rather than
// hand-written permission lists, so a role edit that breaks a route is caught.
//
// An unknown role code yields an EMPTY set, mirroring identity-service: a role
// that does not exist grants nothing, which is what makes such a token safe.
func builtinRolePermissions(scope rbac.Scope, roleCode string) []string {
	for _, tpl := range rbac.BuiltinRoleTemplates {
		if tpl.Scope == scope && tpl.Code == roleCode {
			return tpl.Permissions
		}
	}
	return []string{}
}

// newTestResolver builds an authz.Resolver preloaded with the snapshot the real
// identity-service would return for (userID, tenantID, role).
//
// Tenant-scoped only: no workspace bindings are granted, which matches what
// these middleware tests exercise.
func newTestResolver(
	t *testing.T, tenantID, userID, role string,
) *authz.Resolver {
	t.Helper()

	resolver := authz.NewTestResolver(time.Minute)
	resolver.Preload(userID, tenantID, &authz.Snapshot{
		UserID:               userID,
		TenantID:             tenantID,
		TenantRole:           role,
		TenantPermissions:    builtinRolePermissions(rbac.ScopeTenant, role),
		WorkspaceRoles:       map[string]string{},
		WorkspacePermissions: map[string][]string{},
		PermsVersion:         1,
	})
	return resolver
}
