package rbac

import (
	"sync"
	"time"
)

// defaultCacheTTL bounds how long a resolved role or permission set is reused.
// Role edits and membership changes bump the tenant version for immediate
// invalidation, so the TTL only guards against changes made outside this
// process (e.g. another replica).
const defaultCacheTTL = 60 * time.Second

// maxCacheEntries caps memory usage; the cache is dropped wholesale when the
// limit is hit because rebuilding it costs a single indexed query per key.
const maxCacheEntries = 20000

// permEntry is a cached role -> permission set lookup.
type permEntry struct {
	version uint64
	expires time.Time
	perms   map[string]struct{}
}

// roleEntry is a cached workspace binding lookup. found=false caches the
// "no binding" answer as well, so repeated denied requests stay cheap.
type roleEntry struct {
	version uint64
	expires time.Time
	role    string
	found   bool
}

// cache is a process-local, tenant-versioned TTL cache. Permission checks read
// from it on the hot path, so lookups must stay allocation-free.
type cache struct {
	mu       sync.RWMutex
	ttl      time.Duration
	versions map[string]uint64
	perms    map[string]permEntry
	wsRoles  map[string]roleEntry
}

func newCache(ttl time.Duration) *cache {
	if ttl <= 0 {
		ttl = defaultCacheTTL
	}
	return &cache{
		ttl:      ttl,
		versions: make(map[string]uint64),
		perms:    make(map[string]permEntry),
		wsRoles:  make(map[string]roleEntry),
	}
}

// version returns the current invalidation counter of a tenant.
func (c *cache) version(tenantID string) uint64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.versions[tenantID]
}

// bump invalidates every cached entry of a tenant.
func (c *cache) bump(tenantID string) {
	c.mu.Lock()
	c.versions[tenantID]++
	c.mu.Unlock()
}

// permKey and wsKey deliberately avoid a struct key so the maps stay simple.
func permKey(tenantID string, scope Scope, roleCode string) string {
	return tenantID + "|" + string(scope) + "|" + roleCode
}

func wsKey(tenantID, workspaceID, userID string) string {
	return tenantID + "|" + workspaceID + "|" + userID
}

func (c *cache) getPermissions(tenantID string, scope Scope, roleCode string) (map[string]struct{}, bool) {
	key := permKey(tenantID, scope, roleCode)
	c.mu.RLock()
	entry, ok := c.perms[key]
	current := c.versions[tenantID]
	c.mu.RUnlock()
	if !ok || entry.version != current || time.Now().After(entry.expires) {
		return nil, false
	}
	return entry.perms, true
}

func (c *cache) setPermissions(tenantID string, scope Scope, roleCode string, perms map[string]struct{}) {
	key := permKey(tenantID, scope, roleCode)
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.perms) >= maxCacheEntries {
		c.perms = make(map[string]permEntry)
	}
	c.perms[key] = permEntry{
		version: c.versions[tenantID],
		expires: time.Now().Add(c.ttl),
		perms:   perms,
	}
}

func (c *cache) getWorkspaceRole(tenantID, workspaceID, userID string) (string, bool, bool) {
	key := wsKey(tenantID, workspaceID, userID)
	c.mu.RLock()
	entry, ok := c.wsRoles[key]
	current := c.versions[tenantID]
	c.mu.RUnlock()
	if !ok || entry.version != current || time.Now().After(entry.expires) {
		return "", false, false
	}
	return entry.role, entry.found, true
}

func (c *cache) setWorkspaceRole(tenantID, workspaceID, userID, role string, found bool) {
	key := wsKey(tenantID, workspaceID, userID)
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.wsRoles) >= maxCacheEntries {
		c.wsRoles = make(map[string]roleEntry)
	}
	c.wsRoles[key] = roleEntry{
		version: c.versions[tenantID],
		expires: time.Now().Add(c.ttl),
		role:    role,
		found:   found,
	}
}
