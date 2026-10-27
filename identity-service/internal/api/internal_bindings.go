// Package api — internal endpoints for authorization BINDING MUTATIONS.
//
// Why these exist (design 06 P2/P7 prerequisite):
//
// The gateway used to own the `workspace_members` table and wrote bindings
// directly. That made identity-service and the gateway two writers of one
// dataset, which blocks the P7 Global/Cell database split: `pg_dump
// --schema=identity` cannot safely move a schema that another service still
// writes to. These endpoints let the gateway ask identity-service to perform
// the mutation instead, leaving exactly ONE writer.
//
// The gateway remains the AUTHORIZER: it decides whether the caller may grant
// or revoke, using the snapshot it already holds. This surface performs the
// mutation and enforces only the DOMAIN invariants the gateway cannot know
// (role assignability, "a workspace never loses its last owner", target must
// be a tenant member). The `actor_id` carried in each request is recorded in
// the audit trail, so an internal call is never anonymous.
//
// Guarded by the shared internal token like every other /internal route, and
// unreachable from the user side because the path is not under /api/v1.
package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/auth"
	"github.com/datacode/identity-service/internal/rbac"
)

// BindingHandler serves the internal binding-mutation surface.
type BindingHandler struct {
	auth   *auth.Service
	rbac   *rbac.Service
	logger *zap.Logger
}

func NewBindingHandler(
	authService *auth.Service, rbacService *rbac.Service, logger *zap.Logger,
) *BindingHandler {
	return &BindingHandler{auth: authService, rbac: rbacService, logger: logger}
}

// Routes registers the binding endpoints onto the caller's internal router, so
// they inherit its token guard rather than repeating it.
func (h *BindingHandler) Routes(r chi.Router) {
	r.Get("/tenants/{tenantID}/members", h.ListTenantMembers)
	r.Get("/tenants/{tenantID}/workspaces/{workspaceID}/members", h.ListWorkspaceMembers)
	r.Post("/tenants/{tenantID}/workspaces/{workspaceID}/members", h.GrantWorkspaceMember)
	r.Delete("/tenants/{tenantID}/workspaces/{workspaceID}/members/{userID}", h.RevokeWorkspaceMember)
	r.Delete("/tenants/{tenantID}/workspaces/{workspaceID}/bindings", h.PurgeWorkspace)
	r.Get("/tenants/{tenantID}/roles/{scope}/{roleCode}/permissions", h.RolePermissions)
	r.Get("/tenants/{tenantID}/rbac/role-drift", h.BuiltinRoleDrift)
}

// BuiltinRoleDrift lists the tenant's built-in roles that are missing a
// permission their current template grants.
//
// Why an endpoint and not only a log line: the platform's upgrade rule is "never
// overwrite a tenant's edit", so a permission added to a role template does not
// reach a role that tenant customized. Without a way to ask, that backlog is
// invisible — a newly shipped permission point stays ungrantable for those
// tenants and the symptom is a 403 nobody can explain. Read-only on purpose:
// granting it is an administrator's decision, not an upgrade side effect.
func (h *BindingHandler) BuiltinRoleDrift(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	drift, err := h.rbac.BuiltinRoleDrift(r.Context(), tenantID)
	if err != nil {
		h.logger.Error("internal_role_drift_failed",
			zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"role drift lookup failed"}`, http.StatusInternalServerError)
		return
	}
	if drift == nil {
		drift = []rbac.RoleDrift{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"drift": drift})
}

// ListTenantMembers returns the tenant's member directory (id, username, role).
//
// The gateway needs it to render workspace member names and to verify that a
// grant target belongs to the tenant.
func (h *BindingHandler) ListTenantMembers(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	members, err := h.auth.ListMembers(r.Context(), tenantID)
	if err != nil {
		h.logger.Error("internal_tenant_members_failed",
			zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"tenant member lookup failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"members": members})
}

// ListWorkspaceMembers returns the role bindings of one workspace.
func (h *BindingHandler) ListWorkspaceMembers(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	workspaceID := chi.URLParam(r, "workspaceID")

	bindings, err := h.rbac.ListWorkspaceMembers(r.Context(), tenantID, workspaceID)
	if err != nil {
		h.logger.Error("internal_workspace_members_failed",
			zap.String("workspace_id", workspaceID), zap.Error(err))
		http.Error(w, `{"error":"workspace member lookup failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"members": bindings})
}

// grantRequest is the body of a workspace grant.
//
// Owner is the "creator" flavour: it skips role validation and the tenant
// membership probe because the actor is the freshly created workspace's owner
// by construction, and it is the one grant that must not require an existing
// binding to succeed.
type grantRequest struct {
	ActorID  string `json:"actor_id"`
	UserID   string `json:"user_id"`
	RoleCode string `json:"role_code"`
	Owner    bool   `json:"owner"`
}

// GrantWorkspaceMember binds a tenant member to a workspace role.
func (h *BindingHandler) GrantWorkspaceMember(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	workspaceID := chi.URLParam(r, "workspaceID")

	var req grantRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid body"}`, http.StatusBadRequest)
		return
	}
	if req.UserID == "" {
		http.Error(w, `{"error":"user_id is required"}`, http.StatusBadRequest)
		return
	}

	ctx := r.Context()
	if req.Owner {
		if err := h.rbac.GrantWorkspaceOwner(ctx, tenantID, workspaceID, req.UserID); err != nil {
			h.writeBindingError(w, err, "owner grant failed")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"workspace_id": workspaceID,
			"user_id":      req.UserID,
			"role_code":    rbac.RoleWorkspaceOwner,
		})
		return
	}

	// A workspace binding may only be given to an account that is a member of
	// the same tenant; otherwise a tenant admin could grant access to a
	// stranger's user id.
	inTenant, err := h.auth.HasMember(ctx, tenantID, req.UserID)
	if err != nil {
		h.logger.Error("internal_tenant_membership_probe_failed",
			zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"tenant membership check failed"}`, http.StatusInternalServerError)
		return
	}
	if !inTenant {
		h.logger.Warn("internal_grant_rejected_foreign_user",
			zap.String("tenant_id", tenantID), zap.String("workspace_id", workspaceID))
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "not_tenant_member",
		})
		return
	}

	roleCode := req.RoleCode
	if roleCode == "" {
		roleCode = rbac.RoleWorkspaceViewer
	}
	if err := h.rbac.GrantWorkspaceMember(
		ctx, req.ActorID, tenantID, workspaceID, req.UserID, roleCode,
	); err != nil {
		h.writeBindingError(w, err, "grant failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"workspace_id": workspaceID,
		"user_id":      req.UserID,
		"role_code":    roleCode,
	})
}

// RevokeWorkspaceMember removes one workspace binding.
func (h *BindingHandler) RevokeWorkspaceMember(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	workspaceID := chi.URLParam(r, "workspaceID")
	userID := chi.URLParam(r, "userID")
	actorID := r.URL.Query().Get("actor_id")

	if err := h.rbac.RevokeWorkspaceMember(
		r.Context(), actorID, tenantID, workspaceID, userID,
	); err != nil {
		h.writeBindingError(w, err, "revoke failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"success": true})
}

// PurgeWorkspace drops every binding of a deleted workspace.
func (h *BindingHandler) PurgeWorkspace(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	workspaceID := chi.URLParam(r, "workspaceID")
	actorID := r.URL.Query().Get("actor_id")

	if err := h.rbac.PurgeWorkspace(r.Context(), actorID, tenantID, workspaceID); err != nil {
		h.writeBindingError(w, err, "purge failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"success": true})
}

// RolePermissions returns the effective permission set of one role.
//
// Needed for payload enrichment (the "permissions" field of a workspace
// object) for roles the caller does not personally hold — the authorization
// snapshot only carries the roles the caller is bound to.
func (h *BindingHandler) RolePermissions(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	scope := rbac.Scope(chi.URLParam(r, "scope"))
	roleCode := chi.URLParam(r, "roleCode")

	if !rbac.IsValidScope(string(scope)) {
		http.Error(w, `{"error":"unsupported scope"}`, http.StatusBadRequest)
		return
	}

	permissions, err := h.rbac.EffectivePermissions(r.Context(), tenantID, scope, roleCode)
	if err != nil {
		h.logger.Error("internal_role_permissions_failed",
			zap.String("tenant_id", tenantID), zap.String("role_code", roleCode), zap.Error(err))
		http.Error(w, `{"error":"role permission lookup failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"role_code":   roleCode,
		"scope":       string(scope),
		"permissions": permissions,
	})
}

// writeBindingError maps domain invariant violations onto the SAME status and
// code the user-facing surface emits for them, so the answer is accurate whether
// the gateway forwards it verbatim or re-derives it from the code. (The mapping
// previously disagreed with itself — role_not_found answered 400 here but 404
// everywhere a user can see — and the only reason nobody noticed was that the
// gateway's decoder keyed on the code string alone.)
func (h *BindingHandler) writeBindingError(w http.ResponseWriter, err error, fallback string) {
	switch {
	case errors.Is(err, rbac.ErrLastOwnerLocked):
		writeJSON(w, http.StatusConflict, map[string]string{"error": "last_owner_locked"})
	case errors.Is(err, rbac.ErrBindingNotFound):
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "member_not_found"})
	case errors.Is(err, rbac.ErrRoleNotFound):
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "role_not_found"})
	default:
		h.logger.Error("internal_binding_mutation_failed", zap.Error(err))
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fallback})
	}
}
