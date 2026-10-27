package leaserouter

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/shared/lease"
)

// The behaviour these tests pin: a runtime fleet that runs NO lease subsystem
// (`lease.enabled: false`, the default of a single-replica dev stack) answers
// every placement with 503. That is a standing deployment state, not a fault —
// yet the router used to retry it on every miss, which meant one wasted HTTP
// round trip on the hot path of every file read plus two warning lines per
// request describing the configuration rather than a problem.
//
// The requests themselves always succeeded: the stable-hash degradation is the
// correct and intended mode there. Only the retrying and the noise were wrong.

// TestSubsystemOffIsProbedOnceNotPerRequest is the regression.
func TestSubsystemOffIsProbedOnceNotPerRequest(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: &LeaseSubsystemOffError{}}
	router := newRouter(redis, acquirer)

	for i := 0; i < 8; i++ {
		// Each iteration is a distinct working copy, so the routing cache
		// cannot be what suppresses the placement attempts.
		path := "/var/lib/datacode/workcopies/t1/ws-t1-shop/u1/envs/dev" + string(rune('a'+i))
		addr, err := router.Resolve(context.Background(), path)
		if err != nil {
			t.Fatalf("Resolve #%d errored = %v, want stable-hash degradation", i, err)
		}
		if addr != "http://10.0.0.1:8002" {
			t.Fatalf("Resolve #%d = %q, want the hashed replica", i, addr)
		}
	}

	if acquirer.calls != 1 {
		t.Errorf("acquire calls = %d, want 1 (a standing deployment state must "+
			"not be re-probed on every request)", acquirer.calls)
	}
}

// TestSubsystemOffIsReProbedAfterTheWindow guards the other half: the state is
// remembered, never assumed permanent. Enabling leases must take effect without
// a gateway restart.
func TestSubsystemOffIsReProbedAfterTheWindow(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: &LeaseSubsystemOffError{}}
	router := newRouter(redis, acquirer)

	if _, err := router.Resolve(context.Background(), copyPath); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Simulate the probe window elapsing rather than sleeping for it.
	router.mu.Lock()
	router.subsystemOffUntil = time.Now().Add(-time.Second)
	router.mu.Unlock()
	router.Invalidate(copyPath)

	if _, err := router.Resolve(context.Background(), copyPath); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if acquirer.calls != 2 {
		t.Errorf("acquire calls = %d, want 2 (the state must expire)", acquirer.calls)
	}
}

// TestPlacementSuccessClearsTheOffState pins the recovery path: once the fleet
// answers a placement, the router must stop skipping placements immediately
// rather than waiting out the remaining window.
func TestPlacementSuccessClearsTheOffState(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	router := newRouter(redis, nil)

	// Enter the "off" state, then let a successful placement clear it.
	router.notePlacementUnavailable()
	if router.placementWorthTrying() {
		t.Fatal("the off state was not recorded")
	}
	router.notePlacementAvailable()
	if !router.placementWorthTrying() {
		t.Error("a successful placement must re-enable placement attempts")
	}
}

// TestOtherAcquireFailuresAreStillRetried is the important boundary: only the
// "no subsystem" answer is a standing state. A transport hiccup or a bug must
// keep being retried, because the next request may well succeed — caching THAT
// would turn a blip into a self-inflicted outage of the lease mechanism.
func TestOtherAcquireFailuresAreStillRetried(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: errors.New("placement unreachable")}
	router := newRouter(redis, acquirer)

	for i := 0; i < 3; i++ {
		path := "/var/lib/datacode/workcopies/t1/ws-t1-shop/u1/envs/dev" + string(rune('a'+i))
		if _, err := router.Resolve(context.Background(), path); err != nil {
			t.Fatalf("Resolve #%d errored = %v", i, err)
		}
	}
	if acquirer.calls != 3 {
		t.Errorf("acquire calls = %d, want 3 (a transient failure must be retried)",
			acquirer.calls)
	}
}

// TestCapacityRefusalIsNotMistakenForSubsystemOff keeps the two typed verdicts
// apart. A 429 must still propagate as an error on every request: caching it as
// "placement unavailable" would silently route around the capacity governance
// the runtime just exercised.
func TestCapacityRefusalIsNotMistakenForSubsystemOff(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: &LeaseUnavailableError{RetryAfter: "30"}}
	router := newRouter(redis, acquirer)

	for i := 0; i < 3; i++ {
		router.Invalidate(copyPath)
		_, err := router.Resolve(context.Background(), copyPath)
		var unavailable *LeaseUnavailableError
		if !errors.As(err, &unavailable) {
			t.Fatalf("Resolve #%d err = %v, want the propagated capacity refusal", i, err)
		}
	}
	if acquirer.calls != 3 {
		t.Errorf("acquire calls = %d, want 3 (a capacity verdict is per-request)",
			acquirer.calls)
	}
	if !router.placementWorthTrying() {
		t.Error("a capacity refusal must not be recorded as 'no lease subsystem'")
	}
}

// TestAcquirerMaps503ToTheTypedSubsystemError pins the wire contract at the
// source: the runtime answers 503 when its lease subsystem is not running (see
// src/api/rest/leases.py::_server), and that must arrive typed — the router's
// whole decision hangs on telling it apart from other failures.
func TestAcquirerMaps503ToTheTypedSubsystemError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(
				`{"error_code":"SERVICE_UNAVAILABLE","message":"lease subsystem is not running"}`))
		}))
	t.Cleanup(server.Close)

	acquirer := NewHTTPAcquirer(server.URL, "tok", zap.NewNop())
	key, err := lease.KeyFromWorkingCopyPath(copyPath)
	if err != nil {
		t.Fatalf("bad fixture: %v", err)
	}

	_, err = acquirer.Acquire(context.Background(), key)
	var subsystemOff *LeaseSubsystemOffError
	if !errors.As(err, &subsystemOff) {
		t.Fatalf("err = %v, want *LeaseSubsystemOffError", err)
	}
}
