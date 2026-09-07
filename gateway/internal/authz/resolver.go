// Package authz resolves authorization snapshots from identity-service and
// decides locally (design 02 §2.2, ADR-007).
//
// Why a snapshot client and not a per-check RPC: the gateway authorizes EVERY
// request, so asking identity "can this user do X" per request would put it on
// the hot path of the whole platform. Instead one snapshot per (user, tenant)
// is fetched, cached briefly, and every decision is answered from it in
// process.
//
// Cache correctness rests on two mechanisms, not on the TTL alone:
//
//   - `perms_version`: the caller's JWT carries the tenant's authorization
//     generation at issue time. A snapshot minted under an older generation is
//     discarded, so a revocation takes effect on the caller's next request
//     rather than after the TTL.
//   - a short TTL bounds staleness for everything version-independent (a
//     workspace binding added for a user who never re-logs in).
//
// Degradation is deliberately FAIL CLOSED: when identity is unreachable and
// nothing is cached, the resolver reports an error and the middleware denies.
// Serving a request whose permissions cannot be established is the one failure
// mode this component must never have.
package authz

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sync"
	"time"

	"go.uber.org/zap"
)

// Snapshot mirrors identity-service's authorization payload.
//
// Field shapes are the frozen contract (design 08 §1): workspace bindings are
// keyed by workspace, their permissions keyed by ROLE, so a caller with many
// workspaces on one role transfers a single permission set.
type Snapshot struct {
	UserID               string              `json:"user_id"`
	TenantID             string              `json:"tenant_id"`
	TenantRole           string              `json:"tenant_role"`
	TenantPermissions    []string            `json:"tenant_permissions"`
	WorkspaceRoles       map[string]string   `json:"workspace_roles"`
	WorkspacePermissions map[string][]string `json:"workspace_permissions"`
	PermsVersion         int64               `json:"perms_version"`
}

// HasTenantPermission reports whether the tenant role grants a permission.
func (s *Snapshot) HasTenantPermission(permission string) bool {
	return contains(s.TenantPermissions, permission)
}

// WorkspaceRole returns the caller's role in a workspace ("" when not a member).
func (s *Snapshot) WorkspaceRole(workspaceID string) string {
	return s.WorkspaceRoles[workspaceID]
}

// HasWorkspacePermission reports whether a workspace role grants a permission.
func (s *Snapshot) HasWorkspacePermission(roleCode, permission string) bool {
	return contains(s.WorkspacePermissions[roleCode], permission)
}

// PermissionsForWorkspaceRole returns the effective set of a workspace role.
func (s *Snapshot) PermissionsForWorkspaceRole(roleCode string) []string {
	return s.WorkspacePermissions[roleCode]
}

// CanInAnyWorkspace reports whether the permission holds in at least one of
// the caller's workspaces.
//
// Answered from the snapshot instead of a dedicated query: the bindings and
// their permission sets are already here, which is precisely why the snapshot
// carries both.
func (s *Snapshot) CanInAnyWorkspace(permission string) bool {
	for _, roleCode := range s.WorkspaceRoles {
		if contains(s.WorkspacePermissions[roleCode], permission) {
			return true
		}
	}
	return false
}

func contains(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}

// entry is one cached snapshot.
type entry struct {
	snapshot  *Snapshot
	expiresAt time.Time
}

// Resolver fetches and caches snapshots.
type Resolver struct {
	baseURL string
	token   string
	client  *http.Client
	ttl     time.Duration
	logger  *zap.Logger

	mu    sync.RWMutex
	cache map[string]entry

	// inflight collapses concurrent misses for the same key into one request,
	// so a burst on a cold cache does not stampede identity-service.
	inflight sync.Map // map[string]*call
}

type call struct {
	done     chan struct{}
	snapshot *Snapshot
	err      error
}

// NewResolver builds a resolver. An empty baseURL disables it, which the
// middleware treats as "deny everything that needs authorization" — the safe
// reading of a missing identity endpoint.
func NewResolver(baseURL, token string, ttl time.Duration, logger *zap.Logger) *Resolver {
	if ttl <= 0 {
		ttl = 60 * time.Second
	}
	if logger == nil {
		logger = zap.NewNop()
	}
	return &Resolver{
		baseURL: baseURL,
		token:   token,
		ttl:     ttl,
		logger:  logger,
		cache:   map[string]entry{},
		client: &http.Client{
			// Bounded: authorization sits in the request path, so a stalled
			// identity-service must fail fast rather than hang the gateway.
			Timeout: 5 * time.Second,
		},
	}
}

// Enabled reports whether an identity endpoint is configured.
func (r *Resolver) Enabled() bool { return r.baseURL != "" && r.token != "" }

// Resolve returns the authorization snapshot for a caller.
//
// tokenPermsVersion is the `perms_version` claim of the caller's JWT; a cached
// snapshot older than it is refetched. Pass 0 when the token predates the
// claim, which falls back to TTL-only staleness.
func (r *Resolver) Resolve(
	ctx context.Context, userID, tenantID string, tokenPermsVersion int64,
) (*Snapshot, error) {
	if !r.Enabled() {
		return nil, fmt.Errorf("identity resolver is not configured")
	}
	key := userID + "\x00" + tenantID

	if cached, ok := r.cached(key, tokenPermsVersion); ok {
		return cached, nil
	}
	return r.fetchOnce(ctx, key, userID, tenantID)
}

// Invalidate drops a caller's cached snapshot. Called after the gateway itself
// mutates bindings (workspace member add/remove) so the effect is immediate
// for that caller rather than TTL-delayed.
func (r *Resolver) Invalidate(userID, tenantID string) {
	r.mu.Lock()
	delete(r.cache, userID+"\x00"+tenantID)
	r.mu.Unlock()
}

// InvalidateTenant drops every cached snapshot of a tenant. Used after a role
// or role-permission change, which affects all of its members at once.
func (r *Resolver) InvalidateTenant(tenantID string) {
	suffix := "\x00" + tenantID
	r.mu.Lock()
	for key := range r.cache {
		if len(key) > len(suffix) && key[len(key)-len(suffix):] == suffix {
			delete(r.cache, key)
		}
	}
	r.mu.Unlock()
}

func (r *Resolver) cached(key string, tokenPermsVersion int64) (*Snapshot, bool) {
	r.mu.RLock()
	found, ok := r.cache[key]
	r.mu.RUnlock()
	if !ok || time.Now().After(found.expiresAt) {
		return nil, false
	}
	// A token minted under a newer generation than the cached snapshot means
	// the tenant's authorization changed after it was taken.
	if tokenPermsVersion > 0 && found.snapshot.PermsVersion < tokenPermsVersion {
		return nil, false
	}
	return found.snapshot, true
}

// fetchOnce performs the HTTP fetch, collapsing concurrent duplicates.
func (r *Resolver) fetchOnce(
	ctx context.Context, key, userID, tenantID string,
) (*Snapshot, error) {
	pending := &call{done: make(chan struct{})}
	actual, loaded := r.inflight.LoadOrStore(key, pending)
	leader := actual.(*call)
	if loaded {
		// Another goroutine is already fetching this key: wait for its result.
		select {
		case <-leader.done:
			return leader.snapshot, leader.err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}

	snapshot, err := r.fetch(ctx, userID, tenantID)
	leader.snapshot, leader.err = snapshot, err
	close(leader.done)
	r.inflight.Delete(key)

	if err == nil {
		r.mu.Lock()
		r.cache[key] = entry{snapshot: snapshot, expiresAt: time.Now().Add(r.ttl)}
		r.mu.Unlock()
	}
	return snapshot, err
}

func (r *Resolver) fetch(ctx context.Context, userID, tenantID string) (*Snapshot, error) {
	endpoint := fmt.Sprintf("%s/internal/authz/resolve?user_id=%s&tenant_id=%s",
		r.baseURL, url.QueryEscape(userID), url.QueryEscape(tenantID))

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Internal-Token", r.token)

	resp, err := r.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("reach identity-service: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("identity-service returned %d", resp.StatusCode)
	}
	var snapshot Snapshot
	if err := json.NewDecoder(resp.Body).Decode(&snapshot); err != nil {
		return nil, fmt.Errorf("decode authz snapshot: %w", err)
	}
	if snapshot.WorkspaceRoles == nil {
		snapshot.WorkspaceRoles = map[string]string{}
	}
	if snapshot.WorkspacePermissions == nil {
		snapshot.WorkspacePermissions = map[string][]string{}
	}
	return &snapshot, nil
}
