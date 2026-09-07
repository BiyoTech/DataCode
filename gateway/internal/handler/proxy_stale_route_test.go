package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"go.uber.org/zap"
)

// leaseNotHeldBody is the platform error shape the runtime emits when the
// gateway's cached lease route points at a replica that no longer owns the
// working copy (design 08 §7).
const leaseNotHeldBody = `{"error_code":"LEASE_NOT_HELD","message":"This runtime does not hold the lease for the requested working copy; the gateway must re-resolve the lease and retry"}`

// TestLeaseNotHeldRetriesOnFreshRoute is the contract's core behaviour: the
// runtime says "not here", the gateway drops the cached route, re-resolves and
// forwards once more — and the client sees only the second attempt's answer.
func TestLeaseNotHeldRetriesOnFreshRoute(t *testing.T) {
	var staleHits, freshHits int32

	// The replica the cached route (wrongly) points at.
	stale := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			atomic.AddInt32(&staleHits, 1)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			w.Write([]byte(leaseNotHeldBody))
		}))
	defer stale.Close()

	// The replica that actually holds the lease after the hand-over.
	fresh := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			atomic.AddInt32(&freshHits, 1)
			w.Write([]byte(`{"ok":true}`))
		}))
	defer fresh.Close()

	resolutions := int32(0)
	invalidations := int32(0)
	target := ProxyTarget{
		BaseURL: stale.URL,
		ResolveUpstream: func(*http.Request) (string, error) {
			// First resolution returns the stale pod (as the cache would
			// have), the re-resolution after invalidation finds the holder.
			if atomic.AddInt32(&resolutions, 1) == 1 {
				return stale.URL, nil
			}
			return fresh.URL, nil
		},
		OnForwardFailure: func(*http.Request) {
			atomic.AddInt32(&invalidations, 1)
		},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet,
		"/api/v1/git/status?workspace_path=/base/t1/ws-t1-shop/u1/envs/dev", nil)

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 after the retry; body %q",
			recorder.Code, recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), `"ok":true`) {
		t.Errorf("client saw %q, want the fresh replica's answer",
			recorder.Body.String())
	}
	if hits := atomic.LoadInt32(&staleHits); hits != 1 {
		t.Errorf("stale replica hits = %d, want 1", hits)
	}
	if hits := atomic.LoadInt32(&freshHits); hits != 1 {
		t.Errorf("fresh replica hits = %d, want 1", hits)
	}
	if inv := atomic.LoadInt32(&invalidations); inv != 1 {
		t.Errorf("OnForwardFailure calls = %d, want 1 (invalidate before retry)", inv)
	}
}

// TestLeaseNotHeldRetriesAtMostOnce pins the retry bound. A second verdict
// means the lease subsystem cannot settle a route — relaying it is honest,
// retrying in a loop would turn a Redis hiccup into a request storm.
func TestLeaseNotHeldRetriesAtMostOnce(t *testing.T) {
	var hits int32
	upstream := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			atomic.AddInt32(&hits, 1)
			w.WriteHeader(http.StatusConflict)
			w.Write([]byte(leaseNotHeldBody))
		}))
	defer upstream.Close()

	invalidations := int32(0)
	target := ProxyTarget{
		BaseURL: upstream.URL,
		OnForwardFailure: func(*http.Request) {
			atomic.AddInt32(&invalidations, 1)
		},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/v1/git/status", nil)

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusConflict {
		t.Fatalf("status = %d, want the relayed 409", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "LEASE_NOT_HELD") {
		t.Errorf("body should carry the upstream verdict, got %q",
			recorder.Body.String())
	}
	if hits := atomic.LoadInt32(&hits); hits != 2 {
		t.Errorf("upstream hits = %d, want exactly 2 (initial + one retry)", hits)
	}
}

// TestForeignConflictIsRelayedUntouched guards the discrimination: a 409 that
// is NOT the stale-route verdict (an ENVIRONMENT_BUSY, a merge conflict) is a
// real answer for the client, and retrying or rewriting it would corrupt
// semantics the runtime owns.
func TestForeignConflictIsRelayedUntouched(t *testing.T) {
	var hits int32
	upstream := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			atomic.AddInt32(&hits, 1)
			w.WriteHeader(http.StatusConflict)
			w.Write([]byte(`{"error_code":"ENVIRONMENT_BUSY","message":"busy"}`))
		}))
	defer upstream.Close()

	invalidations := int32(0)
	target := ProxyTarget{
		BaseURL: upstream.URL,
		OnForwardFailure: func(*http.Request) {
			atomic.AddInt32(&invalidations, 1)
		},
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/v1/git/status", nil)

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409 relayed", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "ENVIRONMENT_BUSY") {
		t.Errorf("body = %q, want the original upstream body", recorder.Body.String())
	}
	if hits := atomic.LoadInt32(&hits); hits != 1 {
		t.Errorf("upstream hits = %d, want 1 (no retry for foreign conflicts)", hits)
	}
	if inv := atomic.LoadInt32(&invalidations); inv != 0 {
		t.Errorf("OnForwardFailure calls = %d, want 0", inv)
	}
}

// TestLeaseNotHeldReplaysTheRequestBody guards the retried POST carrying the
// same payload: the first attempt consumed the request stream, and a retry
// that silently sends an empty body would commit nothing while reporting
// success.
func TestLeaseNotHeldReplaysTheRequestBody(t *testing.T) {
	var bodies []string
	var attempts int32

	stale := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			buf := make([]byte, 128)
			n, _ := r.Body.Read(buf)
			bodies = append(bodies, string(buf[:n]))
			atomic.AddInt32(&attempts, 1)
			w.WriteHeader(http.StatusConflict)
			w.Write([]byte(leaseNotHeldBody))
		}))
	defer stale.Close()

	fresh := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			buf := make([]byte, 128)
			n, _ := r.Body.Read(buf)
			bodies = append(bodies, string(buf[:n]))
			atomic.AddInt32(&attempts, 1)
			w.Write([]byte(`{"committed":true}`))
		}))
	defer fresh.Close()

	resolutions := int32(0)
	target := ProxyTarget{
		BaseURL: stale.URL,
		ResolveUpstream: func(*http.Request) (string, error) {
			if atomic.AddInt32(&resolutions, 1) == 1 {
				return stale.URL, nil
			}
			return fresh.URL, nil
		},
	}

	payload := `{"workspace_path":"/base/t1/ws/u1/envs/dev","message":"commit me"}`
	request := httptest.NewRequest(http.MethodPost, "/api/v1/git/commit",
		strings.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()

	GenericProxy(target, zap.NewNop())(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body %q", recorder.Code, recorder.Body.String())
	}
	if n := atomic.LoadInt32(&attempts); n != 2 {
		t.Fatalf("attempts = %d, want 2", n)
	}
	if len(bodies) != 2 || bodies[0] != payload || bodies[1] != payload {
		t.Errorf("bodies = %#v, want the payload on both attempts", bodies)
	}
}

// TestErrorCodeOfExtraction pins the verdict parser on its own: the retry
// decision must be made on a real error_code field, never guessed from a
// status alone or a non-JSON body.
func TestErrorCodeOfExtraction(t *testing.T) {
	cases := []struct {
		name string
		body string
		want string
	}{
		{"platform error shape", leaseNotHeldBody, "LEASE_NOT_HELD"},
		{"empty body", "", ""},
		{"html from a misrouted proxy", "<html>502 Bad Gateway</html>", ""},
		{"truncated json", `{"error_code":"LEASE_NOT`, ""},
		{"unrelated json", `{"message":"busy"}`, ""},
	}
	for _, tc := range cases {
		if got := errorCodeOf([]byte(tc.body)); got != tc.want {
			t.Errorf("%s: errorCodeOf(%q) = %q, want %q",
				tc.name, tc.body, got, tc.want)
		}
	}
}
