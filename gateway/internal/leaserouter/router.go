// Package leaserouter resolves which workspace-runtime replica owns a working
// copy, so requests reach the ONE instance allowed to write it
// (design 03 §3.3, design 06 P4a).
//
// Why the gateway must participate
// --------------------------------
// The lease's guarantee is worthless if traffic is load balanced: every other
// P4 simplification (replica-private storage, in-process environment locks,
// uninvalidated SQLMesh context caches) is licensed by the fact that exactly one
// replica writes a given working copy. Load-balanced routing would let a second
// replica serve a request for a copy it does not own, and nothing downstream
// would notice until the working copy was corrupted.
//
// Resolution order, and why each step exists
// ------------------------------------------
//  1. local cache (TTL ~10s) — a Redis round trip per request would put the
//     coordination backend on the hot path of every file read.
//  2. Redis lookup — the authoritative answer.
//  3. acquire — a miss means nobody holds it yet; control-service places it.
//  4. stable-hash fallback — see the degradation note below.
//
// Degradation when Redis is unreachable
// -------------------------------------
// The gateway falls back to hashing the lease key over the known replica set.
// This is NOT as safe as a lease and the design says so (design 05 §5.2): while
// the replica count is stable it routes every request for one copy to one
// replica, which preserves the invariant; if the count changes during the outage
// two replicas can be chosen for the same copy, and the runtime's flock is the
// last line of defence. The alternative — failing every request — turns a
// degraded coordination backend into a total outage, which is worse for a
// dependency the platform is explicitly designed to survive losing.
package leaserouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/shared/lease"
)

// CacheTTL bounds how stale a routing decision may be.
//
// Short on purpose: a lease can move (drain, crash, rebalance), and a stale
// entry sends writes to a replica that no longer owns the copy. Ten seconds is
// well inside the lease TTL, so a moved lease is picked up long before the old
// holder's claim could expire and be regranted.
const CacheTTL = 10 * time.Second

// SubsystemProbeInterval bounds how long the router trusts a "the fleet is not
// running leases" answer before asking again.
//
// The state it caches is a deployment fact, not a transient one, so the value
// only needs to be short enough that ENABLING leases takes effect without a
// gateway restart. One minute keeps the re-probe cost at one request per minute
// (instead of one per file read) while making the switch-on latency negligible
// next to the rollout that performs it.
const SubsystemProbeInterval = time.Minute

// ErrUnavailable means no replica could be resolved for a working copy.
//
// Surfaced as 503 rather than silently routing to the Service: a request that
// reaches an arbitrary replica would violate the single-writer invariant, and a
// user retrying a moment later is a far better outcome than a corrupted
// worktree.
var ErrUnavailable = errors.New("leaserouter: no runtime available for this working copy")

// RedisClient is the subset of the gateway's Redis wrapper this package needs.
// Narrow on purpose: it keeps the router testable without a Redis server, and
// makes it obvious that routing never WRITES lease state — only the holder does.
type RedisClient interface {
	Enabled() bool
	Get(ctx context.Context, segments ...string) (string, error)
	SMembers(ctx context.Context, segments ...string) ([]string, error)
}

// Acquirer asks the control plane to place a lease that nobody holds yet.
type Acquirer interface {
	Acquire(ctx context.Context, key lease.Key) (lease.Record, error)
}

type cacheEntry struct {
	addr      string
	expiresAt time.Time
}

// Router resolves working copies to runtime addresses.
type Router struct {
	redis    RedisClient
	acquirer Acquirer
	// fallbackURL is the load-balanced Service address, used only when leases
	// are disabled entirely (pre-P4a deployments).
	fallbackURL string
	logger      *zap.Logger

	mu    sync.RWMutex
	cache map[string]cacheEntry

	// subsystemOffUntil is when the router will next attempt a placement after
	// the fleet reported that it runs no lease subsystem. Zero means "placement
	// is believed available". Guarded by mu together with the cache: both are
	// routing state read on the same path.
	subsystemOffUntil time.Time
}

// New builds a Router. A nil redis client or a nil acquirer is valid and means
// "that resolution step is unavailable"; the router degrades accordingly.
func New(
	redisClient RedisClient, acquirer Acquirer, fallbackURL string, logger *zap.Logger,
) *Router {
	return &Router{
		redis:       redisClient,
		acquirer:    acquirer,
		fallbackURL: strings.TrimSuffix(fallbackURL, "/"),
		logger:      logger,
		cache:       make(map[string]cacheEntry),
	}
}

// Enabled reports whether lease-aware routing is active. When false the router
// always returns the load-balanced fallback, reproducing pre-P4a behaviour.
func (r *Router) Enabled() bool {
	return r != nil && r.redis != nil && r.redis.Enabled()
}

// Resolve returns the base URL of the replica that owns a working copy.
//
// A malformed path is NOT an error here: many routes carry no workspace_path at
// all (workspace listing, credentials), and those are correctly served by any
// replica. Callers pass what they have; the router decides whether a lease
// applies.
func (r *Router) Resolve(ctx context.Context, workspacePath string) (string, error) {
	if !r.Enabled() {
		return r.fallbackURL, nil
	}

	key, err := lease.KeyFromWorkingCopyPath(workspacePath)
	if err != nil {
		// Not a leasable unit: any replica may serve it.
		return r.fallbackURL, nil
	}

	if addr, ok := r.cached(key); ok {
		return addr, nil
	}

	// Authoritative lookup.
	addr, err := r.lookup(ctx, key)
	if err == nil && addr != "" {
		r.store(key, addr)
		return addr, nil
	}
	if err != nil && !errors.Is(err, errRedisUnavailable) {
		r.logger.Warn("lease_lookup_failed",
			zap.String("lease_key", key.String()), zap.Error(err))
	}

	// Redis is up but nobody holds this copy: ask for a placement.
	if err == nil && r.acquirer != nil && r.placementWorthTrying() {
		record, acquireErr := r.acquirer.Acquire(ctx, key)
		if acquireErr == nil {
			r.notePlacementAvailable()
			resolved := lease.NormalizeAddr(record.RuntimeAddr)
			r.store(key, resolved)
			r.logger.Info("lease_acquired_for_routing",
				zap.String("lease_key", key.String()),
				zap.String("runtime_id", record.RuntimeID))
			return resolved, nil
		}
		// A capacity verdict is final for this request: degrading to the
		// stable hash would route to an instance the placement service just
		// refused, which is precisely what the quota exists to prevent. The
		// typed error carries the contract's 429 + Retry-After to the client.
		var unavailable *LeaseUnavailableError
		if errors.As(acquireErr, &unavailable) {
			r.logger.Warn("lease_acquire_capacity_refused",
				zap.String("lease_key", key.String()),
				zap.String("retry_after", unavailable.RetryAfter))
			return "", acquireErr
		}
		// The fleet runs no lease subsystem. A standing deployment state, so it
		// is recorded once and re-probed on a timer rather than retried per
		// request; the degradation below is the intended behaviour for it.
		var subsystemOff *LeaseSubsystemOffError
		if errors.As(acquireErr, &subsystemOff) {
			r.notePlacementUnavailable()
		} else {
			r.logger.Warn("lease_acquire_failed",
				zap.String("lease_key", key.String()), zap.Error(acquireErr))
		}
	}

	// Coordination unavailable: hash over the live replica set. Correct while
	// the replica count is stable; the runtime's flock covers the rest.
	if addr := r.stableHash(ctx, key); addr != "" {
		// Not logged while the fleet is known to run no lease subsystem: there
		// the degradation is the permanent, intended mode and the reason was
		// already stated once. Every other route here is a genuine coordination
		// failure and stays warned about per occurrence.
		if r.placementWorthTrying() {
			r.logger.Warn("lease_routing_degraded",
				zap.String("lease_key", key.String()),
				zap.String("mode", "stable_hash"),
				zap.String("risk", "replica-count changes may pick a second writer; "+
					"the runtime flock is the remaining guard"))
		}
		return addr, nil
	}

	return "", fmt.Errorf("%w: %s", ErrUnavailable, key.String())
}

// placementWorthTrying reports whether a placement attempt can succeed.
//
// False only while a "the fleet runs no lease subsystem" answer is still fresh.
// Read on every miss, so it takes the read lock and nothing else.
func (r *Router) placementWorthTrying() bool {
	r.mu.RLock()
	until := r.subsystemOffUntil
	r.mu.RUnlock()
	return until.IsZero() || time.Now().After(until)
}

// notePlacementUnavailable records that the fleet runs no lease subsystem.
//
// Logged once per probe window rather than per request: the message describes
// the deployment, and repeating it for every file read drowns the log in a line
// that never changes. A single-replica dev stack (leases off by default) sits in
// this state permanently and must stay quiet.
func (r *Router) notePlacementUnavailable() {
	r.mu.Lock()
	first := r.subsystemOffUntil.IsZero()
	r.subsystemOffUntil = time.Now().Add(SubsystemProbeInterval)
	r.mu.Unlock()

	if !first {
		return
	}
	r.logger.Warn("lease_placement_unavailable",
		zap.String("reason", "the runtime fleet reports no lease subsystem "+
			"(lease.enabled is false, or it has no internal token)"),
		zap.String("effect", "working-copy requests are routed by stable hash; "+
			"safe while the replica count is stable, and the runtime flock is "+
			"the remaining guard"),
		zap.Duration("reprobe_after", SubsystemProbeInterval))
}

// notePlacementAvailable clears the "no lease subsystem" state after a
// placement succeeds, so enabling leases needs no gateway restart.
func (r *Router) notePlacementAvailable() {
	r.mu.RLock()
	off := !r.subsystemOffUntil.IsZero()
	r.mu.RUnlock()
	if !off {
		return
	}

	r.mu.Lock()
	r.subsystemOffUntil = time.Time{}
	r.mu.Unlock()
	r.logger.Info("lease_placement_restored")
}

// Invalidate drops a cached decision.
//
// Called when a forward FAILS: the cheapest explanation for a connection error
// is that the holder went away, and continuing to send that copy's traffic to a
// dead pod for the rest of the TTL would extend one pod's failure into a
// ten-second outage for that user.
func (r *Router) Invalidate(workspacePath string) {
	key, err := lease.KeyFromWorkingCopyPath(workspacePath)
	if err != nil {
		return
	}
	r.mu.Lock()
	delete(r.cache, key.String())
	r.mu.Unlock()
}

var errRedisUnavailable = errors.New("leaserouter: redis unavailable")

// lookup reads the authoritative lease record.
//
// Returns ("", nil) for a clean miss — nobody holds the lease — and
// errRedisUnavailable when the backend itself could not answer. The distinction
// matters: a miss should trigger an acquire, an outage must not (control-service
// needs the same Redis to place a lease).
func (r *Router) lookup(ctx context.Context, key lease.Key) (string, error) {
	raw, err := r.redis.Get(ctx, key.RedisKeySegments()...)
	if err != nil {
		return "", fmt.Errorf("%w: %v", errRedisUnavailable, err)
	}
	if raw == "" {
		return "", nil
	}

	record, err := lease.ParseRecord(raw)
	if err != nil {
		// Malformed record: treat as a miss rather than routing on a guess.
		return "", err
	}
	return lease.NormalizeAddr(record.RuntimeAddr), nil
}

// stableHash picks a replica deterministically from the live runtime set.
//
// The pick itself lives in the shared lease package because agent-service must
// reach the SAME replica for the same working copy: two independent hash
// implementations would disagree the moment either changed, and disagreement
// here means two writers on one Git worktree.
func (r *Router) stableHash(ctx context.Context, key lease.Key) string {
	members, err := r.redis.SMembers(ctx, lease.RuntimeIndexKeySegments()...)
	if err != nil || len(members) == 0 {
		// Without a replica set there is nothing to hash over. The
		// load-balanced Service is the only remaining option, and it is
		// acceptable only because the runtime's flock still refuses a second
		// mount — the request will fail cleanly rather than corrupt anything.
		return r.fallbackURL
	}

	addrs := r.runtimeAddrs(ctx, members)
	if len(addrs) == 0 {
		return r.fallbackURL
	}
	return lease.StableHashPick(key.String(), addrs)
}

// runtimeAddrs reads the addresses of live replicas, skipping any whose
// heartbeat has expired or which is draining.
func (r *Router) runtimeAddrs(ctx context.Context, runtimeIDs []string) []string {
	addrs := make([]string, 0, len(runtimeIDs))
	for _, id := range runtimeIDs {
		raw, err := r.redis.Get(ctx, lease.RuntimeKeySegments(id)...)
		if err != nil || raw == "" {
			// Expired heartbeat: the index is a set and cannot expire
			// per-member, so a stale id is normal and simply skipped.
			continue
		}
		var heartbeat struct {
			Addr     string `json:"addr"`
			Draining bool   `json:"draining"`
		}
		if json.Unmarshal([]byte(raw), &heartbeat) != nil || heartbeat.Addr == "" {
			continue
		}
		if heartbeat.Draining {
			// A draining replica keeps its existing leases but must not be
			// handed new work by the fallback path.
			continue
		}
		addrs = append(addrs, lease.NormalizeAddr(heartbeat.Addr))
	}
	return addrs
}

func (r *Router) cached(key lease.Key) (string, bool) {
	r.mu.RLock()
	entry, ok := r.cache[key.String()]
	r.mu.RUnlock()
	if !ok || time.Now().After(entry.expiresAt) {
		return "", false
	}
	return entry.addr, true
}

func (r *Router) store(key lease.Key, addr string) {
	r.mu.Lock()
	r.cache[key.String()] = cacheEntry{addr: addr, expiresAt: time.Now().Add(CacheTTL)}
	r.mu.Unlock()
}

// StatusCodeFor maps a routing failure to an HTTP status.
//
// 503 with no Retry-After: the lease usually settles within one request, so a
// client retry is expected to succeed immediately, and advertising a delay would
// make the UI wait for something that is already resolved. A capacity verdict
// (LeaseUnavailableError) is the exception: the runtime advertised a real
// window, and the client must honour it.
func StatusCodeFor(err error) int {
	var unavailable *LeaseUnavailableError
	if errors.As(err, &unavailable) {
		return http.StatusTooManyRequests
	}
	if errors.Is(err, ErrUnavailable) {
		return http.StatusServiceUnavailable
	}
	return http.StatusBadGateway
}
