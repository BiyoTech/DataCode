package authz

import "time"

// Preload seeds a snapshot into the cache, bypassing the HTTP fetch.
//
// Exists for TESTS and for a controlled warm-up: the middleware's decision
// logic is worth testing without standing up identity-service, and a preloaded
// entry exercises exactly the same code path as a fetched one.
//
// Not a general-purpose setter: entries still expire on the normal TTL and are
// still discarded when the caller's token carries a newer perms_version, so a
// preload cannot mask a real authorization change.
func (r *Resolver) Preload(userID, tenantID string, snapshot *Snapshot) {
	if snapshot == nil {
		return
	}
	if snapshot.UserID == "" {
		snapshot.UserID = userID
	}
	if snapshot.TenantID == "" {
		snapshot.TenantID = tenantID
	}
	if snapshot.WorkspaceRoles == nil {
		snapshot.WorkspaceRoles = map[string]string{}
	}
	if snapshot.WorkspacePermissions == nil {
		snapshot.WorkspacePermissions = map[string][]string{}
	}

	r.mu.Lock()
	r.cache[userID+"\x00"+tenantID] = entry{
		snapshot:  snapshot,
		expiresAt: time.Now().Add(r.ttl),
	}
	r.mu.Unlock()
}

// NewTestResolver builds a resolver that never performs HTTP calls.
//
// It is "enabled" so the middleware treats it as configured, but every lookup
// must be satisfied by a Preload — an unseeded key surfaces as unavailable,
// which is precisely the fail-closed behaviour under test.
func NewTestResolver(ttl time.Duration) *Resolver {
	r := NewResolver("http://identity.invalid", "test-token", ttl, nil)
	return r
}
