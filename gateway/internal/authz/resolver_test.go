package authz

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.uber.org/zap"
)

// stubIdentity serves snapshots and counts how often it is asked, which is how
// the caching behaviour is observed.
func stubIdentity(t *testing.T, snapshot Snapshot) (*httptest.Server, *int64) {
	t.Helper()
	var calls int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Internal-Token") != "tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		atomic.AddInt64(&calls, 1)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(snapshot)
	}))
	t.Cleanup(server.Close)
	return server, &calls
}

func TestResolveCachesWithinTTL(t *testing.T) {
	server, calls := stubIdentity(t, Snapshot{
		TenantRole:        "admin",
		TenantPermissions: []string{"tenant:member:write"},
		PermsVersion:      3,
	})
	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	ctx := context.Background()

	for i := 0; i < 5; i++ {
		if _, err := resolver.Resolve(ctx, "u1", "t1", 3); err != nil {
			t.Fatalf("resolve #%d: %v", i, err)
		}
	}
	if got := atomic.LoadInt64(calls); got != 1 {
		t.Errorf("identity called %d times, want 1 (the rest must be cached)", got)
	}
}

// The version check is what makes a cached snapshot safe to hold: a token
// minted after a permission change must not be answered from a pre-change
// snapshot.
func TestResolveRefetchesOnNewerPermsVersion(t *testing.T) {
	server, calls := stubIdentity(t, Snapshot{
		TenantRole:   "admin",
		PermsVersion: 1,
	})
	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	ctx := context.Background()

	if _, err := resolver.Resolve(ctx, "u1", "t1", 1); err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	// The caller now presents a token from a newer generation.
	if _, err := resolver.Resolve(ctx, "u1", "t1", 7); err != nil {
		t.Fatalf("second resolve: %v", err)
	}
	if got := atomic.LoadInt64(calls); got != 2 {
		t.Errorf("identity called %d times, want 2 (the version bump must invalidate)", got)
	}
}

func TestResolveExpiresAfterTTL(t *testing.T) {
	server, calls := stubIdentity(t, Snapshot{TenantRole: "admin", PermsVersion: 1})
	resolver := NewResolver(server.URL, "tok", 20*time.Millisecond, zap.NewNop())
	ctx := context.Background()

	if _, err := resolver.Resolve(ctx, "u1", "t1", 0); err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	time.Sleep(40 * time.Millisecond)
	if _, err := resolver.Resolve(ctx, "u1", "t1", 0); err != nil {
		t.Fatalf("second resolve: %v", err)
	}
	if got := atomic.LoadInt64(calls); got != 2 {
		t.Errorf("identity called %d times, want 2 (the entry must expire)", got)
	}
}

func TestInvalidateDropsEntry(t *testing.T) {
	server, calls := stubIdentity(t, Snapshot{TenantRole: "admin", PermsVersion: 1})
	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	ctx := context.Background()

	if _, err := resolver.Resolve(ctx, "u1", "t1", 0); err != nil {
		t.Fatalf("resolve: %v", err)
	}
	resolver.Invalidate("u1", "t1")
	if _, err := resolver.Resolve(ctx, "u1", "t1", 0); err != nil {
		t.Fatalf("resolve after invalidate: %v", err)
	}
	if got := atomic.LoadInt64(calls); got != 2 {
		t.Errorf("identity called %d times, want 2 after an explicit invalidate", got)
	}
}

func TestInvalidateTenantDropsEveryMember(t *testing.T) {
	server, calls := stubIdentity(t, Snapshot{TenantRole: "admin", PermsVersion: 1})
	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	ctx := context.Background()

	for _, user := range []string{"u1", "u2", "u3"} {
		if _, err := resolver.Resolve(ctx, user, "t1", 0); err != nil {
			t.Fatalf("resolve %s: %v", user, err)
		}
	}
	// A resolve for another tenant must SURVIVE the tenant-scoped purge.
	if _, err := resolver.Resolve(ctx, "u1", "t2", 0); err != nil {
		t.Fatalf("resolve other tenant: %v", err)
	}
	before := atomic.LoadInt64(calls)

	resolver.InvalidateTenant("t1")
	for _, user := range []string{"u1", "u2", "u3"} {
		if _, err := resolver.Resolve(ctx, user, "t1", 0); err != nil {
			t.Fatalf("re-resolve %s: %v", user, err)
		}
	}
	if _, err := resolver.Resolve(ctx, "u1", "t2", 0); err != nil {
		t.Fatalf("re-resolve other tenant: %v", err)
	}
	// Three refetches for t1, none for t2.
	if got := atomic.LoadInt64(calls) - before; got != 3 {
		t.Errorf("refetched %d times, want 3 (t2 must stay cached)", got)
	}
}

// Fail closed: with no identity endpoint every lookup must error so the
// middleware denies, rather than silently allowing.
func TestResolveFailsWhenNotConfigured(t *testing.T) {
	resolver := NewResolver("", "", time.Minute, zap.NewNop())
	if resolver.Enabled() {
		t.Fatal("a resolver without a URL must not report itself enabled")
	}
	if _, err := resolver.Resolve(context.Background(), "u1", "t1", 0); err == nil {
		t.Error("an unconfigured resolver must fail rather than allow")
	}
}

func TestResolvePropagatesUpstreamFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)

	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	if _, err := resolver.Resolve(context.Background(), "u1", "t1", 0); err == nil {
		t.Error("an upstream 500 must surface as an error, not an empty snapshot")
	}
}

// Concurrent misses for one key must collapse into a single fetch, otherwise a
// cold cache under load would stampede identity-service.
func TestConcurrentMissesCollapseIntoOneFetch(t *testing.T) {
	var calls int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt64(&calls, 1)
		time.Sleep(30 * time.Millisecond) // widen the race window
		_ = json.NewEncoder(w).Encode(Snapshot{TenantRole: "admin", PermsVersion: 1})
	}))
	t.Cleanup(server.Close)

	resolver := NewResolver(server.URL, "tok", time.Minute, zap.NewNop())
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := resolver.Resolve(context.Background(), "u1", "t1", 0); err != nil {
				t.Errorf("concurrent resolve: %v", err)
			}
		}()
	}
	wg.Wait()

	if got := atomic.LoadInt64(&calls); got != 1 {
		t.Errorf("identity called %d times, want 1 (in-flight requests must collapse)", got)
	}
}

func TestSnapshotDecisionHelpers(t *testing.T) {
	snapshot := &Snapshot{
		TenantPermissions: []string{"tenant:member:read"},
		WorkspaceRoles:    map[string]string{"ws-a": "engineer", "ws-b": "viewer"},
		WorkspacePermissions: map[string][]string{
			"engineer": {"workspace:code:write"},
			"viewer":   {"workspace:code:read"},
		},
	}

	if !snapshot.HasTenantPermission("tenant:member:read") {
		t.Error("granted tenant permission not reported")
	}
	if snapshot.HasTenantPermission("tenant:member:write") {
		t.Error("ungranted tenant permission reported as granted")
	}
	if role := snapshot.WorkspaceRole("ws-a"); role != "engineer" {
		t.Errorf("workspace role = %q, want engineer", role)
	}
	if snapshot.WorkspaceRole("ws-unknown") != "" {
		t.Error("a non-member workspace must resolve to an empty role")
	}
	if !snapshot.HasWorkspacePermission("engineer", "workspace:code:write") {
		t.Error("engineer must hold workspace:code:write")
	}
	// "in any workspace": write is held via ws-a only, and that is enough.
	if !snapshot.CanInAnyWorkspace("workspace:code:write") {
		t.Error("CanInAnyWorkspace must find the permission held in one workspace")
	}
	if snapshot.CanInAnyWorkspace("workspace:admin") {
		t.Error("CanInAnyWorkspace must not invent permissions")
	}
}
