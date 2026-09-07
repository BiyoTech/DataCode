package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/authz"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/middleware"
	rbac "github.com/datacode/shared/rbac"
)

// The bug these tests pin: creating a workspace granted the owner binding but
// left the creator's CACHED authorization snapshot in place. That snapshot was
// taken before the workspace existed, so it carries no binding for it — and the
// client's very next call is "open the workspace I just created", a
// workspace-scoped route. Decided from the stale snapshot it was denied as
// workspace_not_member, telling the creator they are not a member of their own
// brand-new workspace for as long as the cache TTL lasted (60s by default).
//
// The workspace itself was created successfully, which is what made the failure
// so confusing: an error dialog on top of a completed operation.

const (
	testTenant    = "t1"
	testUser      = "u1"
	testWorkspace = "t1/ws-t1-test"
)

// identityRequest builds a request carrying the identity the gateway's auth
// middleware would have published.
func identityRequest(method, target, body string) *http.Request {
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("{}")
	} else {
		reader = strings.NewReader(body)
	}
	r := httptest.NewRequest(method, target, reader)
	ctx := context.WithValue(r.Context(), middleware.UserIDKey, testUser)
	ctx = context.WithValue(ctx, middleware.TenantIDKey, testTenant)
	return r.WithContext(ctx)
}

// stubControlPlane answers the workspace-creation call the way control-service
// does, and reports whether a compensating delete arrived.
func stubControlPlane(t *testing.T, deleted *bool) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodDelete {
				*deleted = true
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"deleted":true}`))
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"workspace_id":"` + testWorkspace + `","name":"test"}`))
		}))
	t.Cleanup(server.Close)
	return server
}

// stubIdentityBindings accepts the owner grant and serves the owner role's
// permissions, mirroring identity-service's internal binding surface.
func stubIdentityBindings(t *testing.T, grantStatus int) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			if strings.Contains(r.URL.Path, "/permissions") {
				_, _ = w.Write([]byte(`{"permissions":["workspace:read"]}`))
				return
			}
			if grantStatus != http.StatusOK {
				w.WriteHeader(grantStatus)
				_, _ = w.Write([]byte(`{"error":"boom"}`))
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"success":true}`))
		}))
	t.Cleanup(server.Close)
	return server
}

// preCreationSnapshot is what the creator's cache holds while they are creating
// a workspace: a tenant role, and no binding for a workspace that does not
// exist yet.
func preCreationSnapshot() *authz.Snapshot {
	return &authz.Snapshot{
		TenantRole:        "admin",
		TenantPermissions: []string{rbac.PermTenantWorkspaceCreate},
		WorkspaceRoles:    map[string]string{},
	}
}

// TestCreateWorkspaceDropsTheCreatorsStaleSnapshot is the regression: after a
// successful creation the pre-creation snapshot must be gone, so the open that
// follows re-resolves and sees the ownership binding.
func TestCreateWorkspaceDropsTheCreatorsStaleSnapshot(t *testing.T) {
	deleted := false
	control := stubControlPlane(t, &deleted)
	identity := stubIdentityBindings(t, http.StatusOK)

	// A long TTL on purpose: with the invalidation missing, nothing else would
	// evict the entry within a user's session.
	resolver := authz.NewTestResolver(time.Hour)
	resolver.Preload(testUser, testTenant, preCreationSnapshot())
	bindings := authz.NewBindings(identity.URL, "tok", zap.NewNop())
	cfg := &config.Config{
		Services: config.ServicesConfig{ControlServiceURL: control.URL},
	}

	recorder := httptest.NewRecorder()
	CreateWorkspace(cfg, bindings, resolver, zap.NewNop())(
		recorder, identityRequest(http.MethodPost, "/api/v1/workspaces",
			`{"name":"test","dialect":"mysql"}`))

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body %s", recorder.Code, recorder.Body.String())
	}
	if deleted {
		t.Fatal("a successful creation must not be compensated")
	}

	// The cache must no longer answer for this caller. An entry surviving here
	// is exactly the "you are not a member of your own workspace" bug: the
	// resolver would keep serving a snapshot without the new binding.
	if _, err := resolver.Resolve(
		context.Background(), testUser, testTenant, 0,
	); err == nil {
		t.Error("the creator's pre-creation snapshot survived; the open that " +
			"follows would be denied as workspace_not_member")
	}
}

// TestCreateWorkspaceResponseCarriesOwnerCapabilities pins the response
// contract the frontend relies on to render the new workspace as the caller's
// own — the same fields the stale snapshot used to contradict.
func TestCreateWorkspaceResponseCarriesOwnerCapabilities(t *testing.T) {
	deleted := false
	control := stubControlPlane(t, &deleted)
	identity := stubIdentityBindings(t, http.StatusOK)

	resolver := authz.NewTestResolver(time.Minute)
	bindings := authz.NewBindings(identity.URL, "tok", zap.NewNop())
	cfg := &config.Config{
		Services: config.ServicesConfig{ControlServiceURL: control.URL},
	}

	recorder := httptest.NewRecorder()
	CreateWorkspace(cfg, bindings, resolver, zap.NewNop())(
		recorder, identityRequest(http.MethodPost, "/api/v1/workspaces",
			`{"name":"test"}`))

	var payload map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatalf("response is not JSON: %v", err)
	}
	if payload["role"] != rbac.RoleWorkspaceOwner {
		t.Errorf("role = %v, want %q", payload["role"], rbac.RoleWorkspaceOwner)
	}
	if payload["is_owner"] != true {
		t.Errorf("is_owner = %v, want true", payload["is_owner"])
	}
}

// TestCreateWorkspaceKeepsTheSnapshotWhenOwnershipFails guards the other
// direction: a creation that was rolled back must NOT look like a grant. The
// invalidation belongs after the binding succeeded, so a failed create leaves
// the caller's authorization exactly as it was.
func TestCreateWorkspaceKeepsTheSnapshotWhenOwnershipFails(t *testing.T) {
	deleted := false
	control := stubControlPlane(t, &deleted)
	identity := stubIdentityBindings(t, http.StatusInternalServerError)

	resolver := authz.NewTestResolver(time.Hour)
	resolver.Preload(testUser, testTenant, preCreationSnapshot())
	bindings := authz.NewBindings(identity.URL, "tok", zap.NewNop())
	cfg := &config.Config{
		Services: config.ServicesConfig{ControlServiceURL: control.URL},
	}

	recorder := httptest.NewRecorder()
	CreateWorkspace(cfg, bindings, resolver, zap.NewNop())(
		recorder, identityRequest(http.MethodPost, "/api/v1/workspaces",
			`{"name":"test"}`))

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", recorder.Code)
	}
	if !deleted {
		t.Error("a workspace nobody may open must be compensated")
	}
	if _, err := resolver.Resolve(
		context.Background(), testUser, testTenant, 0,
	); err != nil {
		t.Error("a rolled-back creation must leave the cached snapshot intact")
	}
}
