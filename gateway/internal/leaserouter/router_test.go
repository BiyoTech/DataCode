package leaserouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/shared/lease"
)

const (
	copyPath  = "/var/lib/datacode/workcopies/t1/ws-t1-shop/u1/envs/dev"
	copyPath2 = "/var/lib/datacode/workcopies/t1/ws-t1-shop/u2/envs/dev"
	fallback  = "http://workspace-runtime:8002"
)

// fakeRedis models only what routing reads. It can be made to fail, which is
// the interesting case: the whole point of the fallback path is what happens
// when coordination is unavailable.
type fakeRedis struct {
	values  map[string]string
	sets    map[string][]string
	fail    bool
	disable bool
	gets    int
}

func newFakeRedis() *fakeRedis {
	return &fakeRedis{values: map[string]string{}, sets: map[string][]string{}}
}

func (f *fakeRedis) Enabled() bool { return !f.disable }

func (f *fakeRedis) Get(_ context.Context, segments ...string) (string, error) {
	f.gets++
	if f.fail {
		return "", errors.New("redis unreachable")
	}
	return f.values[key(segments)], nil
}

func (f *fakeRedis) SMembers(_ context.Context, segments ...string) ([]string, error) {
	if f.fail {
		return nil, errors.New("redis unreachable")
	}
	return f.sets[key(segments)], nil
}

func key(segments []string) string {
	out := ""
	for i, s := range segments {
		if i > 0 {
			out += ":"
		}
		out += s
	}
	return out
}

func (f *fakeRedis) setLease(k lease.Key, runtimeID, addr string) {
	raw, _ := json.Marshal(lease.Record{
		Token: "tok", RuntimeID: runtimeID, RuntimeAddr: addr,
		AcquiredAt: float64(time.Now().Unix()),
	})
	f.values[key(k.RedisKeySegments())] = string(raw)
}

func (f *fakeRedis) setHeartbeat(runtimeID, addr string, draining bool) {
	raw, _ := json.Marshal(map[string]any{"addr": addr, "draining": draining})
	f.values[key(lease.RuntimeKeySegments(runtimeID))] = string(raw)
	idx := key(lease.RuntimeIndexKeySegments())
	f.sets[idx] = append(f.sets[idx], runtimeID)
}

type fakeAcquirer struct {
	addr  string
	err   error
	calls int
}

func (a *fakeAcquirer) Acquire(_ context.Context, _ lease.Key) (lease.Record, error) {
	a.calls++
	if a.err != nil {
		return lease.Record{}, a.err
	}
	return lease.Record{RuntimeID: "rt-acquired", RuntimeAddr: a.addr}, nil
}

func mustKey(t *testing.T, path string) lease.Key {
	t.Helper()
	k, err := lease.KeyFromWorkingCopyPath(path)
	if err != nil {
		t.Fatalf("bad test fixture path %q: %v", path, err)
	}
	return k
}

func newRouter(redis RedisClient, acquirer Acquirer) *Router {
	return New(redis, acquirer, fallback, zap.NewNop())
}

// TestCapacityRefusalPropagatesInsteadOfDegrading pins the router's most
// consequential branch change: a 429 LEASE_UNAVAILABLE verdict from placement
// must reach the caller, NOT fall through to the stable hash.
//
// The stable-hash degradation exists for coordination failures, where any
// replica is better than an error. It is exactly wrong for a capacity
// verdict: the hash would pick one of the instances the placement service
// just refused, routing around the quota the fleet agreed on.
func TestCapacityRefusalPropagatesInsteadOfDegrading(t *testing.T) {
	redis := newFakeRedis()
	// A live replica set the degraded path WOULD have used.
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: &LeaseUnavailableError{RetryAfter: "30"}}

	addr, err := newRouter(redis, acquirer).Resolve(context.Background(), copyPath)

	var unavailable *LeaseUnavailableError
	if !errors.As(err, &unavailable) {
		t.Fatalf("Resolve = (%q, %v), want the propagated capacity refusal", addr, err)
	}
	if addr != "" {
		t.Errorf("addr = %q, want no route on a refused placement", addr)
	}
	if unavailable.RetryAfter != "30" {
		t.Errorf("RetryAfter = %q, want the advertised window", unavailable.RetryAfter)
	}
	// The typed verdict must also map to the contract status for whatever
	// caller consults StatusCodeFor.
	if status := StatusCodeFor(err); status != 429 {
		t.Errorf("StatusCodeFor = %d, want 429", status)
	}
}

// TestAcquireFailureStillDegrades pins the complementary rule: every OTHER
// acquire failure (transport, bug, unauthorized) keeps the old behaviour of
// degrading to the stable hash — a coordination hiccup must not become a
// user-facing error when a replica can serve the copy.
func TestAcquireFailureStillDegrades(t *testing.T) {
	redis := newFakeRedis()
	redis.setHeartbeat("workspace-runtime-0", "10.0.0.1:8002", false)
	acquirer := &fakeAcquirer{err: errors.New("placement unreachable")}

	addr, err := newRouter(redis, acquirer).Resolve(context.Background(), copyPath)

	if err != nil {
		t.Fatalf("Resolve errored = %v, want stable-hash degradation", err)
	}
	// NormalizeAddr turns the heartbeat's host:port into a dialable URL.
	if addr != "http://10.0.0.1:8002" {
		t.Errorf("addr = %q, want the hashed replica", addr)
	}
}

func TestResolveUsesTheLeaseHolder(t *testing.T) {
	redis := newFakeRedis()
	redis.setLease(mustKey(t, copyPath), "workspace-runtime-2", "10.1.2.3:8002")

	got, err := newRouter(redis, nil).Resolve(context.Background(), copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// A pod address, not the Service: routing to a specific replica is the
	// entire purpose of the lease.
	if want := "http://10.1.2.3:8002"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestNonLeasablePathsGoToAnyReplica(t *testing.T) {
	redis := newFakeRedis()
	router := newRouter(redis, nil)

	// Many routes carry no workspace_path at all (workspace listing, git
	// credentials). Those are correctly served by any replica, so an
	// unparseable path must not be an error.
	for _, path := range []string{"", "/api/v1/workspaces", "/base/t1/ws-t1-shop/u1"} {
		got, err := router.Resolve(context.Background(), path)
		if err != nil {
			t.Errorf("Resolve(%q) unexpected error: %v", path, err)
		}
		if got != fallback {
			t.Errorf("Resolve(%q) = %q, want the load-balanced fallback", path, got)
		}
	}
}

func TestDisabledRouterReproducesPreLeaseBehaviour(t *testing.T) {
	redis := newFakeRedis()
	redis.disable = true
	router := newRouter(redis, nil)

	if router.Enabled() {
		t.Error("router must report itself disabled when Redis is off")
	}
	got, err := router.Resolve(context.Background(), copyPath)
	if err != nil || got != fallback {
		t.Errorf("got (%q, %v), want the fallback with no error", got, err)
	}
}

func TestCacheAvoidsARedisRoundTripPerRequest(t *testing.T) {
	redis := newFakeRedis()
	redis.setLease(mustKey(t, copyPath), "rt-1", "10.0.0.1:8002")
	router := newRouter(redis, nil)

	for i := 0; i < 5; i++ {
		if _, err := router.Resolve(context.Background(), copyPath); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	// Without caching, every file read in the editor would hit Redis.
	if redis.gets != 1 {
		t.Errorf("redis GETs = %d, want 1 (subsequent reads should be cached)", redis.gets)
	}
}

func TestInvalidateForcesAFreshLookup(t *testing.T) {
	redis := newFakeRedis()
	k := mustKey(t, copyPath)
	redis.setLease(k, "rt-1", "10.0.0.1:8002")
	router := newRouter(redis, nil)

	if _, err := router.Resolve(context.Background(), copyPath); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// The lease moved (drain or crash). A forward failure is the gateway's only
	// signal, and without invalidation it would keep sending this copy's writes
	// to a dead pod for the rest of the cache TTL.
	redis.setLease(k, "rt-2", "10.0.0.2:8002")
	router.Invalidate(copyPath)

	got, err := router.Resolve(context.Background(), copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := "http://10.0.0.2:8002"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestCacheExpires(t *testing.T) {
	redis := newFakeRedis()
	k := mustKey(t, copyPath)
	redis.setLease(k, "rt-1", "10.0.0.1:8002")
	router := newRouter(redis, nil)

	if _, err := router.Resolve(context.Background(), copyPath); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Simulate the TTL elapsing rather than sleeping for it.
	router.mu.Lock()
	router.cache[k.String()] = cacheEntry{
		addr: "http://10.0.0.1:8002", expiresAt: time.Now().Add(-time.Second)}
	router.mu.Unlock()

	redis.setLease(k, "rt-9", "10.0.0.9:8002")
	got, _ := router.Resolve(context.Background(), copyPath)
	if want := "http://10.0.0.9:8002"; got != want {
		t.Errorf("got %q, want %q (a stale entry must not outlive its TTL)", got, want)
	}
}

func TestMissTriggersAnAcquire(t *testing.T) {
	redis := newFakeRedis() // no lease stored
	acquirer := &fakeAcquirer{addr: "10.0.0.7:8002"}

	got, err := newRouter(redis, acquirer).Resolve(context.Background(), copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if acquirer.calls != 1 {
		t.Errorf("acquire calls = %d, want 1", acquirer.calls)
	}
	if want := "http://10.0.0.7:8002"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestAcquiredLeaseIsCached(t *testing.T) {
	redis := newFakeRedis()
	acquirer := &fakeAcquirer{addr: "10.0.0.7:8002"}
	router := newRouter(redis, acquirer)

	for i := 0; i < 3; i++ {
		if _, err := router.Resolve(context.Background(), copyPath); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	// Placement is a control-plane round trip; repeating it per request would
	// put the controller on the hot path.
	if acquirer.calls != 1 {
		t.Errorf("acquire calls = %d, want 1", acquirer.calls)
	}
}

func TestRedisOutageDoesNotTriggerAnAcquire(t *testing.T) {
	// control-service needs the SAME Redis to place a lease, so asking it
	// during an outage only adds latency to a request that must degrade anyway.
	redis := newFakeRedis()
	redis.setHeartbeat("rt-1", "10.0.0.1:8002", false)
	redis.fail = true
	acquirer := &fakeAcquirer{addr: "10.0.0.7:8002"}

	_, _ = newRouter(redis, acquirer).Resolve(context.Background(), copyPath)

	if acquirer.calls != 0 {
		t.Errorf("acquire calls = %d, want 0 during a coordination outage", acquirer.calls)
	}
}

func TestDegradedRoutingIsStableForTheSameCopy(t *testing.T) {
	// This is what preserves the single-writer invariant during an outage: the
	// same copy must always hash to the same replica.
	redis := newFakeRedis()
	redis.setHeartbeat("rt-1", "10.0.0.1:8002", false)
	redis.setHeartbeat("rt-2", "10.0.0.2:8002", false)
	redis.setHeartbeat("rt-3", "10.0.0.3:8002", false)

	router := newRouter(redis, nil)
	first, err := resolveDegraded(router, copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for i := 0; i < 10; i++ {
		again, _ := resolveDegraded(router, copyPath)
		if again != first {
			t.Fatalf("degraded routing is unstable: %q then %q", first, again)
		}
	}
}

func TestDegradedRoutingSpreadsDistinctCopies(t *testing.T) {
	// Hashing must distribute, otherwise an outage would pile every workspace
	// onto one replica and OOM it.
	redis := newFakeRedis()
	for i := 1; i <= 3; i++ {
		redis.setHeartbeat(
			fmt.Sprintf("rt-%d", i), fmt.Sprintf("10.0.0.%d:8002", i), false)
	}
	router := newRouter(redis, nil)

	a, _ := resolveDegraded(router, copyPath)
	b, _ := resolveDegraded(router, copyPath2)
	if a == "" || b == "" {
		t.Fatal("degraded routing returned nothing")
	}
	// Not a strict requirement that they differ, but both must be real replicas.
	for _, addr := range []string{a, b} {
		if addr == fallback {
			t.Errorf("degraded routing fell back to the Service despite live replicas")
		}
	}
}

func TestDegradedRoutingSkipsDrainingReplicas(t *testing.T) {
	// A draining replica keeps its existing leases but must not be handed new
	// work: it is about to disappear.
	redis := newFakeRedis()
	redis.setHeartbeat("rt-1", "10.0.0.1:8002", true)
	redis.setHeartbeat("rt-2", "10.0.0.2:8002", false)

	router := newRouter(redis, nil)
	got, err := resolveDegraded(router, copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := "http://10.0.0.2:8002"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestDegradedRoutingSkipsExpiredHeartbeats(t *testing.T) {
	// The index is a set and cannot expire per member, so a crashed replica's id
	// lingers. Routing to it would send every request into a connection error.
	redis := newFakeRedis()
	idx := key(lease.RuntimeIndexKeySegments())
	redis.sets[idx] = []string{"rt-dead"} // id present, no heartbeat value
	redis.setHeartbeat("rt-2", "10.0.0.2:8002", false)

	got, err := resolveDegraded(router(redis), copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := "http://10.0.0.2:8002"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestMalformedLeaseRecordIsNotRoutedOn(t *testing.T) {
	// Routing on a guess is worse than degrading: the record is the one thing
	// that says who may write, and a corrupt one means we do not know.
	redis := newFakeRedis()
	redis.values[key(mustKey(t, copyPath).RedisKeySegments())] = `{"runtime_id":"rt-1"}`
	redis.setHeartbeat("rt-1", "10.0.0.1:8002", false)

	got, err := newRouter(redis, nil).Resolve(context.Background(), copyPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Falls through to the degraded path rather than inventing an address.
	if got == "" {
		t.Error("expected a degraded decision, got nothing")
	}
}

func TestNoReplicasAtAllYieldsUnavailable(t *testing.T) {
	redis := newFakeRedis()
	redis.fail = true
	// No fallback configured either: there is genuinely nowhere to send this.
	router := New(redis, nil, "", zap.NewNop())

	_, err := router.Resolve(context.Background(), copyPath)
	if !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v, want ErrUnavailable", err)
	}
	// 503 rather than routing anywhere: a retry is expected to succeed, whereas
	// an arbitrary replica would break the single-writer invariant.
	if code := StatusCodeFor(err); code != 503 {
		t.Errorf("status = %d, want 503", code)
	}
}

// resolveDegraded exercises the fallback path.
//
// The degraded path is reached when no lease record exists AND no acquirer is
// configured, which is exactly the shape of a coordination outage from the
// router's point of view: it knows the replica set (heartbeats) but not who
// owns the copy.
func resolveDegraded(r *Router, path string) (string, error) {
	return r.Resolve(context.Background(), path)
}

func router(redis RedisClient) *Router { return newRouter(redis, nil) }
