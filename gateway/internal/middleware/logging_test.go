package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	rbac "github.com/datacode/shared/rbac"
)

// loggedRequest returns the fields of the single http_request line emitted while
// serving one request through the real logger + auth chain.
func loggedRequest(t *testing.T, req *http.Request) map[string]any {
	t.Helper()

	core, logs := observer.New(zapcore.InfoLevel)
	logger := zap.New(core)
	issuer := newTestIssuer(t, time.Hour)

	handler := RequestLogger(logger)(
		JWTAuth(issuer, zap.NewNop())(
			http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
			}),
		),
	)
	handler.ServeHTTP(httptest.NewRecorder(), req)

	entries := logs.FilterMessage("http_request").All()
	if len(entries) != 1 {
		t.Fatalf("got %d http_request log lines, want exactly 1", len(entries))
	}
	return entries[0].ContextMap()
}

// TestAccessLogAttributesAuthenticatedRequests is a regression test for a defect
// that made the gateway access log useless for auditing: the logger wraps the
// whole chain (it must, to measure status and duration), so it runs BEFORE
// authentication, and authentication publishes the identity on a derived request
// the logger's closure cannot see. Every line reported an empty user_id.
func TestAccessLogAttributesAuthenticatedRequests(t *testing.T) {
	issuer := newTestIssuer(t, time.Hour)
	token, _, err := issuer.IssueAccessToken("user-1", "alice", "tenant-1", rbac.RoleTenantAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/v1/git/credentials", nil)
	req.Header.Set("Authorization", "Bearer "+token)

	fields := loggedRequest(t, req)
	if fields["user_id"] != "user-1" {
		t.Errorf("user_id = %v, want user-1", fields["user_id"])
	}
	if fields["tenant_id"] != "tenant-1" {
		t.Errorf("tenant_id = %v, want tenant-1", fields["tenant_id"])
	}
	if fields["status"] != int64(http.StatusOK) {
		t.Errorf("status = %v, want 200", fields["status"])
	}
}

// TestAccessLogCoversUnauthenticatedRequests pins the other half of the fix: the
// logger stays OUTERMOST, so a rejected request is still recorded. Moving it
// after authentication would have produced attributed lines at the cost of
// losing every 401 and every public-route request — the opposite of an audit
// trail.
func TestAccessLogCoversUnauthenticatedRequests(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/git/credentials", nil)

	fields := loggedRequest(t, req)
	if fields["status"] != int64(http.StatusUnauthorized) {
		t.Errorf("status = %v, want 401", fields["status"])
	}
	if fields["user_id"] != "" {
		t.Errorf("user_id = %v, want empty for an unauthenticated request", fields["user_id"])
	}
	if fields["trace_id"] == "" {
		t.Error("a rejected request must still carry a trace id")
	}
}

// TestAccessLogPreservesInboundTraceID keeps the cross-service correlation
// working: a trace id supplied by the caller must be reused, not replaced.
func TestAccessLogPreservesInboundTraceID(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/health", nil)
	req.Header.Set("X-Trace-ID", "trace-from-caller")

	fields := loggedRequest(t, req)
	if fields["trace_id"] != "trace-from-caller" {
		t.Errorf("trace_id = %v, want the inbound value", fields["trace_id"])
	}
}

// TestSetRequestIdentityWithoutSlotIsSafe covers handlers exercised without the
// access logger in front of them, which is how most middleware tests are wired.
func TestSetRequestIdentityWithoutSlotIsSafe(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/health", nil)
	SetRequestIdentity(req.Context(), "user-1", "tenant-1") // must not panic
}
