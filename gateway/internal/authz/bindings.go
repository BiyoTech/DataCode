// Package authz — client for identity-service's binding-mutation surface.
//
// Companion to Resolver: where Resolver READS authorization snapshots for
// decisions, Bindings performs the WRITES the gateway used to do against its
// own `workspace_members` table.
//
// Why the gateway no longer writes directly (design 06 P2/P7 prerequisite):
// two writers of one dataset block the Global/Cell database split, because the
// identity schema cannot be moved out from under a service that still writes
// it. The gateway keeps deciding WHETHER a mutation is allowed — it holds the
// route map and the snapshot — and delegates the mutation itself.
//
// Failure handling is deliberately loud: unlike the read path there is no cache
// to fall back on, so an unreachable identity-service surfaces as an error and
// the caller returns 502 rather than reporting a success that never happened.
package authz

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"go.uber.org/zap"
)

// Binding-mutation outcomes the gateway must translate into user-facing
// messages. They mirror the domain invariants identity-service enforces.
var (
	// ErrLastOwnerLocked reports that the mutation would leave a workspace
	// without an owner.
	ErrLastOwnerLocked = errors.New("the last owner of a workspace cannot be removed or downgraded")
	// ErrBindingNotFound reports that the member has no binding to revoke.
	ErrBindingNotFound = errors.New("role binding not found")
	// ErrRoleNotFound reports an unknown or unassignable role code.
	ErrRoleNotFound = errors.New("role not found")
	// ErrNotTenantMember reports that the grant target does not belong to the
	// tenant, so it must not receive a workspace binding.
	ErrNotTenantMember = errors.New("target user is not a member of this tenant")
	// ErrBindingsUnavailable reports that identity-service could not be
	// reached or refused the call. Distinct from the domain errors above: it
	// means the outcome is unknown rather than rejected.
	ErrBindingsUnavailable = errors.New("identity-service bindings API is unavailable")
)

// TenantMember is one row of a tenant's member directory.
//
// The JSON tags mirror identity-service's `auth.MemberInfo` wire format
// (camelCase), which is also what the frontend already consumes.
type TenantMember struct {
	UserID   string `json:"userId"`
	Username string `json:"username"`
	Role     string `json:"role"`
}

// WorkspaceMember is one workspace role binding, matching identity-service's
// `rbac.WorkspaceMember` wire format.
type WorkspaceMember struct {
	UserID   string `json:"userId"`
	RoleCode string `json:"role"`
	// CreatedAt is a UTC RFC3339 string; the frontend renders it in local time.
	CreatedAt string `json:"createdAt"`
}

// Bindings calls identity-service's internal binding API.
type Bindings struct {
	baseURL string
	token   string
	client  *http.Client
	logger  *zap.Logger
}

// NewBindings builds the client. An empty baseURL or token disables it, which
// every caller must treat as "this mutation cannot be performed".
func NewBindings(baseURL, token string, logger *zap.Logger) *Bindings {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &Bindings{
		baseURL: baseURL,
		token:   token,
		logger:  logger,
		client: &http.Client{
			// Bounded: these calls sit inside user-facing requests, so a
			// stalled identity-service must fail fast.
			Timeout: 10 * time.Second,
		},
	}
}

// Enabled reports whether an identity endpoint is configured.
func (b *Bindings) Enabled() bool { return b.baseURL != "" && b.token != "" }

// ListTenantMembers returns the tenant's member directory.
func (b *Bindings) ListTenantMembers(
	ctx context.Context, tenantID string,
) ([]TenantMember, error) {
	var payload struct {
		Members []TenantMember `json:"members"`
	}
	path := fmt.Sprintf("/internal/tenants/%s/members", url.PathEscape(tenantID))
	if err := b.do(ctx, http.MethodGet, path, nil, &payload); err != nil {
		return nil, err
	}
	return payload.Members, nil
}

// ListWorkspaceMembers returns the role bindings of one workspace.
func (b *Bindings) ListWorkspaceMembers(
	ctx context.Context, tenantID, workspaceID string,
) ([]WorkspaceMember, error) {
	var payload struct {
		Members []WorkspaceMember `json:"members"`
	}
	path := fmt.Sprintf("/internal/tenants/%s/workspaces/%s/members",
		url.PathEscape(tenantID), url.PathEscape(workspaceID))
	if err := b.do(ctx, http.MethodGet, path, nil, &payload); err != nil {
		return nil, err
	}
	return payload.Members, nil
}

// GrantWorkspaceOwner records the creator binding of a new workspace.
func (b *Bindings) GrantWorkspaceOwner(
	ctx context.Context, tenantID, workspaceID, userID string,
) error {
	path := fmt.Sprintf("/internal/tenants/%s/workspaces/%s/members",
		url.PathEscape(tenantID), url.PathEscape(workspaceID))
	body := map[string]any{"actor_id": userID, "user_id": userID, "owner": true}
	return b.do(ctx, http.MethodPost, path, body, nil)
}

// GrantWorkspaceMember binds a tenant member to a workspace role.
func (b *Bindings) GrantWorkspaceMember(
	ctx context.Context, actorID, tenantID, workspaceID, userID, roleCode string,
) error {
	path := fmt.Sprintf("/internal/tenants/%s/workspaces/%s/members",
		url.PathEscape(tenantID), url.PathEscape(workspaceID))
	body := map[string]any{
		"actor_id":  actorID,
		"user_id":   userID,
		"role_code": roleCode,
	}
	return b.do(ctx, http.MethodPost, path, body, nil)
}

// RevokeWorkspaceMember removes one workspace binding.
func (b *Bindings) RevokeWorkspaceMember(
	ctx context.Context, actorID, tenantID, workspaceID, userID string,
) error {
	path := fmt.Sprintf("/internal/tenants/%s/workspaces/%s/members/%s?actor_id=%s",
		url.PathEscape(tenantID), url.PathEscape(workspaceID),
		url.PathEscape(userID), url.QueryEscape(actorID))
	return b.do(ctx, http.MethodDelete, path, nil, nil)
}

// PurgeWorkspace drops every binding of a deleted workspace.
func (b *Bindings) PurgeWorkspace(
	ctx context.Context, actorID, tenantID, workspaceID string,
) error {
	path := fmt.Sprintf("/internal/tenants/%s/workspaces/%s/bindings?actor_id=%s",
		url.PathEscape(tenantID), url.PathEscape(workspaceID), url.QueryEscape(actorID))
	return b.do(ctx, http.MethodDelete, path, nil, nil)
}

// RolePermissions returns the effective permission set of one role.
//
// Used to enrich a response with the capabilities of a role the CALLER does
// not hold (the snapshot only carries the caller's own roles).
func (b *Bindings) RolePermissions(
	ctx context.Context, tenantID, scope, roleCode string,
) ([]string, error) {
	var payload struct {
		Permissions []string `json:"permissions"`
	}
	path := fmt.Sprintf("/internal/tenants/%s/roles/%s/%s/permissions",
		url.PathEscape(tenantID), url.PathEscape(scope), url.PathEscape(roleCode))
	if err := b.do(ctx, http.MethodGet, path, nil, &payload); err != nil {
		return nil, err
	}
	return payload.Permissions, nil
}

// do performs one internal call, mapping transport and domain failures onto the
// package's error values.
func (b *Bindings) do(
	ctx context.Context, method, path string, body any, out any,
) error {
	if !b.Enabled() {
		return ErrBindingsUnavailable
	}

	var reader *bytes.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode binding request: %w", err)
		}
		reader = bytes.NewReader(encoded)
	} else {
		reader = bytes.NewReader(nil)
	}

	req, err := http.NewRequestWithContext(ctx, method, b.baseURL+path, reader)
	if err != nil {
		return fmt.Errorf("build binding request: %w", err)
	}
	req.Header.Set("X-Internal-Token", b.token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := b.client.Do(req)
	if err != nil {
		b.logger.Error("identity_bindings_unreachable",
			zap.String("path", path), zap.Error(err))
		return fmt.Errorf("%w: %v", ErrBindingsUnavailable, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode >= 400 {
		return b.decodeError(resp, path)
	}
	if out == nil {
		return nil
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("decode binding response: %w", err)
	}
	return nil
}

// decodeError maps an error response onto a domain error, so the handler can
// render an accurate message instead of a generic failure.
func (b *Bindings) decodeError(resp *http.Response, path string) error {
	var payload struct {
		Error string `json:"error"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&payload)

	switch payload.Error {
	case "last_owner_locked":
		return ErrLastOwnerLocked
	case "member_not_found":
		return ErrBindingNotFound
	case "role_not_found":
		return ErrRoleNotFound
	case "not_tenant_member":
		return ErrNotTenantMember
	}

	b.logger.Error("identity_bindings_rejected",
		zap.String("path", path),
		zap.Int("status", resp.StatusCode),
		zap.String("upstream_error", payload.Error))
	return fmt.Errorf("%w: status %d", ErrBindingsUnavailable, resp.StatusCode)
}
