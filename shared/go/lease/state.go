package lease

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// State is the lifecycle of a lease as the routing layer sees it.
// Mirrors datacode_common.lease.LeaseState.
type State string

const (
	// StateHeld — the holder owns the working copy and serves requests.
	StateHeld State = "held"
	// StateDraining — the holder is shutting down but STILL owns the working
	// copy. It must keep receiving requests: routing elsewhere would create a
	// second writer, and refusing to route would abort the in-flight operation
	// that draining exists to let finish.
	StateDraining State = "draining"
	// StateReleased — ownership is gone; the record is only of historical
	// interest.
	StateReleased State = "released"
)

// IsRoutable reports whether the gateway may forward requests to this holder.
// Draining holders stay routable on purpose — see StateDraining.
func (s State) IsRoutable() bool {
	return s == StateHeld || s == StateDraining
}

// DefaultTTL is the crash-recovery bound, mirroring LEASE_TTL_SECONDS.
//
// It is NOT an operation timeout: the holder renews every TTL/RenewDivisor while
// alive, so a live lease never expires under a long apply. The TTL only decides
// how long a DEAD holder's working copy stays unreachable before another
// instance may take it.
const DefaultTTL = 120 * time.Second

// RenewDivisor mirrors LEASE_RENEW_DIVISOR. Not configurable: a holder renewing
// less than twice per TTL would lose its lease to one dropped packet.
const RenewDivisor = 3

// Record is the lease as it is stored in Redis by the holder.
//
// The JSON tags are the wire contract with the Python side, which writes this
// value in src/lease/holder.py. Only the fields the gateway needs to route are
// modelled; unknown fields are ignored so the holder can add diagnostics without
// coordinating a gateway release.
type Record struct {
	// Token is the ownership nonce. The gateway never writes, so it only reads
	// this for diagnostics; the holder uses it for compare-and-swap.
	Token string `json:"token"`
	// RuntimeID identifies the owning instance (a StatefulSet pod name).
	RuntimeID string `json:"runtime_id"`
	// RuntimeAddr is what the gateway dials — a pod address, bypassing the
	// Service. Routing to a specific pod is the entire point of a lease.
	RuntimeAddr string `json:"runtime_addr"`
	// AcquiredAt is a Unix timestamp in seconds (UTC), matching time.time() on
	// the Python side.
	AcquiredAt float64 `json:"acquired_at"`
}

// ParseRecord decodes a lease record read from Redis.
//
// A record without an address is treated as malformed rather than as "route to
// the Service": silently falling back to load-balanced routing would break the
// single-writer invariant precisely when the lease data is untrustworthy.
func ParseRecord(raw string) (Record, error) {
	var record Record
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		return Record{}, fmt.Errorf("lease: undecodable record: %w", err)
	}
	if record.RuntimeAddr == "" {
		return Record{}, fmt.Errorf("lease: record has no runtime_addr")
	}
	return record, nil
}

// Redis key segments. These are appended to the client's namespace prefix,
// which already contains "{key_prefix}:{cell_id}" on both sides — so a lease in
// cell-0 can never be mistaken for one in cell-1 even on a shared Redis
// instance (design 03 §2).
const (
	// KeyNamespace holds one Record per working copy.
	KeyNamespace = "lease"
	// RuntimeNamespace holds one heartbeat per runtime instance.
	RuntimeNamespace = "rt"
	// RuntimeIndexSegment is the set of live runtime ids. An explicit index
	// rather than "SCAN rt:*": placement runs on the request path when a lease
	// is missed, and SCAN costs time proportional to the whole keyspace.
	RuntimeIndexSegment = "index"
)

// RedisKeySegments returns the segments identifying a lease's Redis key.
//
// Returned as segments rather than a joined string because the namespace prefix
// belongs to the Redis client (redisx.Key), which is the only component that
// knows the configured prefix and cell id.
func (k Key) RedisKeySegments() []string {
	return []string{KeyNamespace, k.String()}
}

// RuntimeKeySegments returns the segments for one runtime's heartbeat key.
func RuntimeKeySegments(runtimeID string) []string {
	return []string{RuntimeNamespace, runtimeID}
}

// RuntimeIndexKeySegments returns the segments for the live-runtime index.
func RuntimeIndexKeySegments() []string {
	return []string{RuntimeNamespace, RuntimeIndexSegment}
}

// NormalizeAddr turns a stored address into a dialable base URL.
//
// The holder stores "host:port" because it does not know how the gateway
// reaches it; the scheme is the gateway's concern. Defaulting to http is
// correct for in-cluster pod-to-pod traffic, and an address that already
// carries a scheme is passed through so a TLS deployment needs no code change.
//
// Mirrors datacode_common.lease.normalize_runtime_addr.
func NormalizeAddr(addr string) string {
	if addr == "" {
		return ""
	}
	if strings.HasPrefix(addr, "http://") || strings.HasPrefix(addr, "https://") {
		return strings.TrimSuffix(addr, "/")
	}
	return "http://" + strings.TrimSuffix(addr, "/")
}

// FNV-1a (32-bit) parameters, spelled out rather than taken from hash/fnv.
//
// The algorithm is part of the CROSS-LANGUAGE contract, not an implementation
// detail: the degraded routing path must map a lease key to the same index of
// the same sorted replica list in Go and in Python, or the two routers would
// pick different replicas for one working copy and put a second writer on it —
// the exact failure the degraded path exists to avoid. Naming the constants here
// keeps that intent visible next to the mirror in
// datacode_common.lease.stable_hash_index.
const (
	fnvOffsetBasis32 uint32 = 0x811c9dc5
	fnvPrime32       uint32 = 0x01000193
)

// StableHashIndex returns the index a lease key maps to over count candidates.
//
// Returns -1 for a non-positive count: there is no valid index, and a caller
// that ignored the sentinel would address element 0 of an empty list.
func StableHashIndex(leaseKey string, count int) int {
	if count <= 0 {
		return -1
	}
	digest := fnvOffsetBasis32
	for i := 0; i < len(leaseKey); i++ {
		digest = (digest ^ uint32(leaseKey[i])) * fnvPrime32
	}
	return int(digest % uint32(count))
}

// StableHashPick chooses one candidate deterministically, sorting first.
//
// The sort is not cosmetic: replica sets come from an unordered Redis SET, and
// two routers reading them in different orders would disagree about who owns a
// working copy while each believed itself stable.
func StableHashPick(leaseKey string, candidates []string) string {
	ordered := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		if candidate != "" {
			ordered = append(ordered, candidate)
		}
	}
	if len(ordered) == 0 {
		return ""
	}
	sort.Strings(ordered)
	return ordered[StableHashIndex(leaseKey, len(ordered))]
}
