package middleware

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/rbac"
)

// Capability headers published to downstream services. They are always
// overwritten (or removed) so a client can never inject its own capabilities.
const (
	HeaderTenantRole    = "X-Tenant-Role"
	HeaderWorkspaceID   = "X-Workspace-ID"
	HeaderWorkspaceRole = "X-Workspace-Role"
	HeaderPermissions   = "X-Granted-Permissions"
)

// capabilityHeaders is the list stripped from every inbound request.
var capabilityHeaders = []string{
	HeaderTenantRole, HeaderWorkspaceID, HeaderWorkspaceRole, HeaderPermissions,
}

// maxAuthzBodyBytes caps how much of a request body is buffered to locate the
// workspace of a request. Larger payloads (file contents) are not inspected.
const maxAuthzBodyBytes = 1 << 20 // 1 MiB

// Authorize is the single authorization enforcement point of the platform.
//
// It runs as a global middleware — before chi matches a route — so every
// endpoint, including the transparent catch-all proxies, is covered. The route
// map declares which permission a request requires and where its scope is
// found; the decision itself is delegated to the RBAC service, which reads
// roles and their permission composition from the database.
//
// On success the resolved capabilities are published into the request context
// and onto trusted headers, so downstream services never have to authorize
// again.
func Authorize(rbacService *rbac.Service, logger *zap.Logger) func(http.Handler) http.Handler {
	sampler := newDenialSampler(20, 10*time.Second)

	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			StripCapabilityHeaders(r)

			// Preflight and public endpoints carry no identity to authorize.
			if r.Method == http.MethodOptions || isPublicPath(r.URL.Path) {
				next.ServeHTTP(w, r)
				return
			}
			userID := GetUserID(r.Context())
			if userID == "" {
				// JWTAuth already rejected anonymous access to private routes.
				next.ServeHTTP(w, r)
				return
			}
			tenantID := GetTenantID(r.Context())
			if tenantID == "" {
				sampler.log(logger, "authz_denied", r, tenantID, userID, "", DenyNoTenant)
				WriteForbidden(w, DenyNoTenant, "")
				return
			}

			rule := rbac.MatchRoute(r.Method, r.URL.Path)
			if rule.Deny {
				sampler.log(logger, "authz_route_not_mapped", r, tenantID, userID, "",
					DenyRouteNotAllowed)
				WriteForbidden(w, DenyRouteNotAllowed, "")
				return
			}

			ctx := r.Context()
			tenantRole := GetRole(ctx)

			// Built-in roles are instantiated lazily per tenant so existing
			// tenants pick up newly released roles without a migration.
			if err := rbacService.EnsureTenantRoles(ctx, tenantID); err != nil {
				logger.Error("authz_tenant_roles_unavailable",
					zap.String("tenant_id", tenantID), zap.Error(err))
				writeAuthError(w, http.StatusInternalServerError, "internal_error",
					"Authorization data is temporarily unavailable.")
				return
			}

			decision, err := decide(ctx, rbacService, r, rule, tenantID, userID, tenantRole)
			if err != nil {
				logger.Error("authz_decision_failed",
					zap.String("path", r.URL.Path),
					zap.String("tenant_id", tenantID), zap.Error(err))
				writeAuthError(w, http.StatusInternalServerError, "internal_error",
					"Authorization check failed.")
				return
			}
			if !decision.allowed {
				sampler.log(logger, "authz_denied", r, tenantID, userID,
					rule.Permission, decision.reason)
				WriteForbidden(w, decision.reason, rule.Permission)
				return
			}

			ctx = context.WithValue(ctx, WorkspaceIDKey, decision.workspaceID)
			ctx = context.WithValue(ctx, WorkspaceRoleKey, decision.workspaceRole)
			ctx = context.WithValue(ctx, PermissionsKey, decision.permissions)

			r.Header.Set(HeaderTenantRole, tenantRole)
			r.Header.Set(HeaderWorkspaceID, decision.workspaceID)
			r.Header.Set(HeaderWorkspaceRole, decision.workspaceRole)
			r.Header.Set(HeaderPermissions, strings.Join(decision.permissions, ","))

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// StripCapabilityHeaders removes authorization headers supplied by the client.
// Only the gateway may set them.
func StripCapabilityHeaders(r *http.Request) {
	for _, header := range capabilityHeaders {
		r.Header.Del(header)
	}
}

// authzDecision is the outcome published to the context and downstream headers.
type authzDecision struct {
	allowed       bool
	workspaceID   string
	workspaceRole string
	// reason explains a rejection so the client can render an actionable
	// message. Meaningless when allowed is true.
	reason DenyReason
	// permissions is the effective set of the scope the request was resolved
	// in: the workspace role when a workspace applies, the tenant role
	// otherwise.
	permissions []string
}

// decide evaluates one request against its rule.
func decide(
	ctx context.Context, rbacService *rbac.Service, r *http.Request,
	rule rbac.Rule, tenantID, userID, tenantRole string,
) (authzDecision, error) {
	tenantPermissions, err := rbacService.EffectivePermissions(
		ctx, tenantID, rbac.ScopeTenant, tenantRole)
	if err != nil {
		return authzDecision{}, err
	}
	decision := authzDecision{
		permissions: tenantPermissions,
		reason:      DenyMissingPermission,
	}

	switch rule.Source {
	case rbac.ScopeSourceNone:
		decision.allowed = true
		return decision, nil

	case rbac.ScopeSourceTenant:
		granted, err := rbacService.Can(ctx, tenantID, rbac.ScopeTenant, tenantRole, rule.Permission)
		if err != nil {
			return authzDecision{}, err
		}
		decision.allowed = granted
		return decision, nil
	}

	// Workspace-scoped rule: locate the workspace the request targets.
	workspaceID, resolved := resolveWorkspaceID(r, rule, tenantID)
	if !resolved {
		if !rule.ScopeOptional {
			// The request does not say which workspace it acts on, so no
			// workspace role can be evaluated.
			decision.reason = DenyWorkspaceUnresolved
			return decision, nil
		}
		// Instance-wide endpoint: the permission must hold in at least one of
		// the caller's workspaces.
		granted, err := rbacService.CanInAnyWorkspace(ctx, tenantID, userID, rule.Permission)
		if err != nil {
			return authzDecision{}, err
		}
		decision.allowed = granted
		return decision, nil
	}

	workspaceRole, err := rbacService.WorkspaceRole(ctx, tenantID, workspaceID, userID)
	if err != nil {
		return authzDecision{}, err
	}
	if workspaceRole == "" {
		// Not a member: the workspace does not exist as far as the caller
		// is concerned.
		decision.reason = DenyWorkspaceNotMember
		return decision, nil
	}
	granted, err := rbacService.Can(ctx, tenantID, rbac.ScopeWorkspace, workspaceRole, rule.Permission)
	if err != nil {
		return authzDecision{}, err
	}
	workspacePermissions, err := rbacService.EffectivePermissions(
		ctx, tenantID, rbac.ScopeWorkspace, workspaceRole)
	if err != nil {
		return authzDecision{}, err
	}

	decision.allowed = granted
	decision.workspaceID = workspaceID
	decision.workspaceRole = workspaceRole
	decision.permissions = workspacePermissions
	return decision, nil
}

// resolveWorkspaceID extracts the workspace a request targets, according to the
// scope source declared by the matched rule. Cross-tenant identifiers are
// rejected because the tenant is encoded in the workspace id itself.
func resolveWorkspaceID(r *http.Request, rule rbac.Rule, tenantID string) (string, bool) {
	switch rule.Source {
	case rbac.ScopeSourceWorkspaceSegment:
		segment, ok := rbac.SegmentAt(r.URL.Path, rule.SegmentIndex)
		if !ok || !rbac.BelongsToTenant(segment, tenantID) {
			return "", false
		}
		return segment, true

	case rbac.ScopeSourceWorkspaceField:
		value := requestField(r, "workspace_id")
		if !rbac.BelongsToTenant(value, tenantID) {
			return "", false
		}
		return value, true

	case rbac.ScopeSourceWorkspacePath:
		return rbac.WorkspaceIDFromPath(requestField(r, "workspace_path"), tenantID)
	}
	return "", false
}

// requestField reads a value from the query string, falling back to the JSON
// body. The body is buffered and restored so downstream handlers and the proxy
// still see the original payload.
func requestField(r *http.Request, field string) string {
	if value := r.URL.Query().Get(field); value != "" {
		return value
	}
	switch r.Method {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
	default:
		return ""
	}
	if r.Body == nil || r.ContentLength == 0 || r.ContentLength > maxAuthzBodyBytes {
		return ""
	}
	if contentType := r.Header.Get("Content-Type"); contentType != "" &&
		!strings.HasPrefix(contentType, "application/json") {
		return ""
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, maxAuthzBodyBytes+1))
	r.Body.Close()
	r.Body = io.NopCloser(bytes.NewReader(raw))
	if err != nil || len(raw) > maxAuthzBodyBytes {
		return ""
	}

	var payload map[string]json.RawMessage
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}
	encoded, present := payload[field]
	if !present {
		return ""
	}
	var value string
	if err := json.Unmarshal(encoded, &value); err != nil {
		return ""
	}
	return value
}

// denialSampler bounds how many denial records are written per time window, so
// a client looping on a forbidden endpoint cannot flood the logs.
type denialSampler struct {
	mu       sync.Mutex
	limit    int
	window   time.Duration
	count    int
	windowAt time.Time
}

func newDenialSampler(limit int, window time.Duration) *denialSampler {
	return &denialSampler{limit: limit, window: window, windowAt: time.Now()}
}

// log records one denial if the current window still has budget.
func (s *denialSampler) log(
	logger *zap.Logger, event string, r *http.Request,
	tenantID, userID, permission string, reason DenyReason,
) {
	s.mu.Lock()
	now := time.Now()
	if now.Sub(s.windowAt) > s.window {
		s.windowAt = now
		s.count = 0
	}
	s.count++
	allowed := s.count <= s.limit
	s.mu.Unlock()

	if !allowed {
		return
	}
	logger.Warn(event,
		zap.String("method", r.Method),
		zap.String("path", r.URL.Path),
		zap.String("tenant_id", tenantID),
		zap.String("user_id", userID),
		zap.String("permission", permission),
		zap.String("reason", string(reason)))
}
