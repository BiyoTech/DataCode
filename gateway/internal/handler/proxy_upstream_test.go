package handler

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.uber.org/zap"
)

// TestUpstreamForFallsBackToBaseURL pins the pre-P4a behaviour: without a
// resolver, or when the resolver declines, the static target is used. This is
// what lets the lease subsystem be switched off by configuration.
func TestUpstreamForFallsBackToBaseURL(t *testing.T) {
	base := "http://workspace-runtime:8002"

	noResolver := ProxyTarget{BaseURL: base}
	if got, err := noResolver.upstreamFor(nil); got != base || err != nil {
		t.Errorf("no resolver: got (%q, %v), want (%q, nil)", got, err, base)
	}

	declines := ProxyTarget{
		BaseURL:         base,
		ResolveUpstream: func(*http.Request) (string, error) { return "", nil },
	}
	if got, err := declines.upstreamFor(nil); got != base || err != nil {
		t.Errorf("declining resolver: got (%q, %v), want (%q, nil)", got, err, base)
	}
}

func TestUpstreamForPrefersTheResolvedReplica(t *testing.T) {
	target := ProxyTarget{
		BaseURL: "http://workspace-runtime:8002",
		ResolveUpstream: func(*http.Request) (string, error) {
			return "http://10.1.2.3:8002", nil
		},
	}
	// A pod address, not the Service: the whole point of a lease.
	if got, _ := target.upstreamFor(nil); got != "http://10.1.2.3:8002" {
		t.Errorf("got %q, want the resolved pod address", got)
	}
}

// TestUnresolvableUpstreamAnswers503 is the important one: the tempting
// implementation falls back to BaseURL when routing fails, which would forward
// the request to an arbitrary replica and put a second writer on one Git
// worktree. Failing the request is the correct outcome.
func TestUnresolvableUpstreamAnswers503(t *testing.T) {
	target := ProxyTarget{
		BaseURL: "http://workspace-runtime:8002",
		ResolveUpstream: func(*http.Request) (string, error) {
			return "", errors.New("no runtime available")
		},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet,
		"/api/v1/git/status?workspace_path=/base/t1/ws-t1-shop/u1/envs/dev", nil)

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 (must not fall back to the Service)",
			recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "retry") {
		t.Errorf("body should invite a retry, got %q", recorder.Body.String())
	}
}

// TestUnresolvableUpstreamDoesNotForwardTheBody guards against the failure being
// detected only AFTER the body was consumed, which would make a retry send an
// empty payload.
func TestUnresolvableUpstreamDoesNotForwardTheBody(t *testing.T) {
	target := ProxyTarget{
		BaseURL: "http://unused",
		ResolveUpstream: func(*http.Request) (string, error) {
			return "", errors.New("no runtime available")
		},
	}

	body := `{"workspace_path":"/base/t1/ws-t1-shop/u1/envs/dev"}`
	request := httptest.NewRequest(http.MethodPost, "/api/v1/git/commit",
		strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", recorder.Code)
	}
	// The body must still be readable: resolution happens before it is consumed.
	remaining := make([]byte, len(body))
	n, _ := request.Body.Read(remaining)
	if n == 0 {
		t.Error("request body was consumed before the routing decision")
	}
}

// TestValidatedProxyResolvesUpstreamPerRequest pins the fix for validated
// routes bypassing lease routing: MakeValidatedProxy used to forward to the
// static BaseURL, which load-balances working-copy reads across replicas —
// the exact behaviour the single-writer invariant forbids. It must honour the
// per-request resolver exactly like GenericProxy.
func TestValidatedProxyResolvesUpstreamPerRequest(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"served_by":"resolved_replica"}`))
		}))
	defer upstream.Close()

	target := ProxyTarget{
		BaseURL: "http://load-balanced-service:8002", // must NOT be used
		ResolveUpstream: func(*http.Request) (string, error) {
			return upstream.URL, nil
		},
	}
	route := ValidatedRoute{
		Method:   "GET",
		Pattern:  "/status",
		Required: []string{"workspace_path"},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet,
		"/api/v1/wip/status?workspace_path=/base/t1/ws-t1-shop/u1/envs/dev", nil)

	MakeValidatedProxy(target, route, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body %q",
			recorder.Code, recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), "resolved_replica") {
		t.Errorf("request did not reach the resolver's replica: %q",
			recorder.Body.String())
	}
}

// TestValidatedProxyUnresolvableAnswers503 pins the same no-fallback rule for
// validated routes: a working-copy read that cannot find its owner must fail,
// not be spread across replicas.
func TestValidatedProxyUnresolvableAnswers503(t *testing.T) {
	target := ProxyTarget{
		BaseURL: "http://workspace-runtime:8002",
		ResolveUpstream: func(*http.Request) (string, error) {
			return "", errors.New("no runtime available")
		},
	}
	route := ValidatedRoute{
		Method:   "GET",
		Pattern:  "/status",
		Required: []string{"workspace_path"},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet,
		"/api/v1/wip/status?workspace_path=/base/t1/ws-t1-shop/u1/envs/dev", nil)

	MakeValidatedProxy(target, route, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 (must not fall back to the Service)",
			recorder.Code)
	}
}

// TestForwardFailureInvalidatesStaleRoute pins the hook the lease router
// depends on: when the resolved upstream cannot be reached, the cached routing
// decision must be invalidated instead of surviving its full TTL.
func TestForwardFailureInvalidatesStaleRoute(t *testing.T) {
	// A server that no longer exists: the address stays valid syntactically,
	// the port is closed, so the transport fails fast.
	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	deadURL := dead.URL
	dead.Close()

	invalidated := 0
	target := ProxyTarget{
		BaseURL: deadURL,
		OnForwardFailure: func(*http.Request) {
			invalidated++
		},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/v1/wip/status", nil)

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", recorder.Code)
	}
	if invalidated != 1 {
		t.Errorf("OnForwardFailure calls = %d, want 1", invalidated)
	}
}
