package leaserouter

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.uber.org/zap"

	"github.com/datacode/shared/lease"
)

func testKey(t *testing.T) lease.Key {
	t.Helper()
	key, err := lease.ParseKey("t1/ws-t1-shop/u1/dev")
	if err != nil {
		t.Fatalf("bad fixture key: %v", err)
	}
	return key
}

// TestAcquireReturnsThePlacedRecord is the happy path: the runtime answers
// {runtime_id, addr} and the gateway routes on the pod address.
func TestAcquireReturnsThePlacedRecord(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/leases/acquire" {
			t.Errorf("path = %q, want the acquire endpoint", r.URL.Path)
		}
		if got := r.Header.Get("X-Internal-Token"); got != "sekrit" {
			t.Errorf("internal token = %q, want the shared secret", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"runtime_id":"workspace-runtime-2","addr":"10.1.2.3:8002"}`))
	}))
	defer server.Close()

	acquirer := NewHTTPAcquirer(server.URL, "sekrit", zap.NewNop())
	record, err := acquirer.Acquire(context.Background(), testKey(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if record.RuntimeAddr != "10.1.2.3:8002" || record.RuntimeID != "workspace-runtime-2" {
		t.Errorf("record = %+v", record)
	}
}

// TestAcquireMapsCapacityRefusalToTheTypedError pins the 429 branch: a
// LEASE_UNAVAILABLE verdict must arrive as LeaseUnavailableError — the type is
// what tells Resolve NOT to degrade to the stable hash, and what carries the
// contract's Retry-After through to the client.
func TestAcquireMapsCapacityRefusalToTheTypedError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "30")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(
			`{"error_code":"LEASE_UNAVAILABLE","message":"no capacity"}`))
	}))
	defer server.Close()

	acquirer := NewHTTPAcquirer(server.URL, "sekrit", zap.NewNop())
	_, err := acquirer.Acquire(context.Background(), testKey(t))

	var unavailable *LeaseUnavailableError
	if !errors.As(err, &unavailable) {
		t.Fatalf("error = %v, want LeaseUnavailableError", err)
	}
	if unavailable.RetryAfter != "30" {
		t.Errorf("RetryAfter = %q, want the runtime's advertised window", unavailable.RetryAfter)
	}
	if unavailable.HTTPStatus() != http.StatusTooManyRequests {
		t.Errorf("HTTPStatus = %d, want 429", unavailable.HTTPStatus())
	}
	if unavailable.ErrorCode() != "LEASE_UNAVAILABLE" {
		t.Errorf("ErrorCode = %q, want the contract literal", unavailable.ErrorCode())
	}
}

// TestAcquireSurfacesErrors pins the contract the router depends on: an error
// return is what tells Resolve to degrade instead of routing on a guess, so
// swallowing failures here would silently disable placement.
func TestAcquireSurfacesErrors(t *testing.T) {
	statuses := map[int]string{
		http.StatusServiceUnavailable: "no instance could take the lease",
		http.StatusConflict:           "lease raced elsewhere",
		http.StatusInternalServerError: "runtime bug",
	}
	for status := range statuses {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
		}))
		acquirer := NewHTTPAcquirer(server.URL, "sekrit", zap.NewNop())
		if _, err := acquirer.Acquire(context.Background(), testKey(t)); err == nil {
			t.Errorf("status %d: expected an error, got none", status)
		}
		server.Close()
	}
}

// TestAcquireRejectsSuccessWithoutAnAddress guards the wire contract: a 200
// with no addr would route traffic to nowhere, and treating it as a placement
// would cache a decision that cannot work.
func TestAcquireRejectsSuccessWithoutAnAddress(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"runtime_id":"rt-1"}`))
	}))
	defer server.Close()

	acquirer := NewHTTPAcquirer(server.URL, "sekrit", zap.NewNop())
	if _, err := acquirer.Acquire(context.Background(), testKey(t)); err == nil {
		t.Error("expected an error for a response without addr")
	}
}

// TestAcquireDisabledWithoutBaseURL keeps the pre-P4a behaviour reachable:
// with no runtime URL configured, misses stay on the stable-hash path.
func TestAcquireDisabledWithoutBaseURL(t *testing.T) {
	if NewHTTPAcquirer("", "tok", zap.NewNop()).Enabled() {
		t.Error("acquirer must be disabled without a base URL")
	}
}
