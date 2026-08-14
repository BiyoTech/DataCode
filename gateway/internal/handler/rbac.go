// Package handler — role management endpoints backed by the RBAC domain.
//
// Roles and their permission composition live in the database and are editable
// per tenant, including the built-in ones. The permission catalog itself is
// read-only: it is declared in code and synchronized on startup, so a caller
// can never invent a permission code.
package handler

import (
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/middleware"
	"github.com/agenticdata/gateway/internal/rbac"
)

// RBACHandler exposes the role management endpoints.
type RBACHandler struct {
	service *rbac.Service
	logger  *zap.Logger
}

// NewRBACHandler builds the role management handler set.
func NewRBACHandler(service *rbac.Service, logger *zap.Logger) *RBACHandler {
	return &RBACHandler{service: service, logger: logger}
}

// roleRequest is the payload for creating and updating a role.
type roleRequest struct {
	Scope       string   `json:"scope"`
	Code        string   `json:"code"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Permissions []string `json:"permissions"`
}

// ListPermissions returns the permission catalog grouped by scope, so the UI can
// render a checkbox matrix without hard-coding any code.
func (h *RBACHandler) ListPermissions(w http.ResponseWriter, r *http.Request) {
	permissions, err := h.service.ListPermissions(r.Context())
	if err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"permissions": permissions})
}

// ListRoles returns the tenant's roles. The optional `scope` filter narrows the
// result to one authorization scope, and `assignable=1` hides roles that are
// granted implicitly (the workspace owner).
func (h *RBACHandler) ListRoles(w http.ResponseWriter, r *http.Request) {
	tenantID := middleware.GetTenantID(r.Context())
	scope := r.URL.Query().Get("scope")
	if scope != "" && !rbac.IsValidScope(scope) {
		writeError(w, http.StatusBadRequest, "validation_failed", "Unsupported scope.")
		return
	}

	var (
		roles []rbac.Role
		err   error
	)
	if r.URL.Query().Get("assignable") == "1" {
		roles, err = h.service.AssignableRoles(r.Context(), tenantID, rbac.Scope(scope))
	} else {
		roles, err = h.service.ListRoles(r.Context(), tenantID, rbac.Scope(scope))
	}
	if err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"roles": roles})
}

// CreateRole adds a custom role to the caller's tenant.
func (h *RBACHandler) CreateRole(w http.ResponseWriter, r *http.Request) {
	var req roleRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	// The tenant always comes from the verified token, never from the body.
	tenantID := middleware.GetTenantID(r.Context())
	actorID := middleware.GetUserID(r.Context())

	role, err := h.service.CreateRole(r.Context(), actorID, tenantID,
		rbac.Scope(req.Scope), req.Code, req.Name, req.Description, req.Permissions)
	if err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	writeJSON(w, http.StatusCreated, role)
}

// UpdateRole renames a role and replaces its permission set. Built-in roles are
// editable this way; only their code is immutable.
func (h *RBACHandler) UpdateRole(w http.ResponseWriter, r *http.Request) {
	var req roleRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	tenantID := middleware.GetTenantID(r.Context())
	actorID := middleware.GetUserID(r.Context())

	role, err := h.service.UpdateRole(r.Context(), actorID, tenantID,
		chi.URLParam(r, "roleID"), req.Name, req.Description, req.Permissions)
	if err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	writeJSON(w, http.StatusOK, role)
}

// DeleteRole removes a custom role that no member is bound to.
func (h *RBACHandler) DeleteRole(w http.ResponseWriter, r *http.Request) {
	tenantID := middleware.GetTenantID(r.Context())
	actorID := middleware.GetUserID(r.Context())

	if err := h.service.DeleteRole(
		r.Context(), actorID, tenantID, chi.URLParam(r, "roleID"),
	); err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"success": true})
}

// writeRBACError maps RBAC domain errors to HTTP codes without leaking
// internals. It is shared with the workspace membership handlers.
func writeRBACError(w http.ResponseWriter, logger *zap.Logger, err error) {
	switch {
	case errors.Is(err, rbac.ErrRoleNotFound):
		writeError(w, http.StatusNotFound, "role_not_found", "Role not found.")
	case errors.Is(err, rbac.ErrBindingNotFound):
		writeError(w, http.StatusNotFound, "member_not_found",
			"This member has no access to the workspace.")
	case errors.Is(err, rbac.ErrRoleExists):
		writeError(w, http.StatusConflict, "role_exists",
			"A role with this code already exists.")
	case errors.Is(err, rbac.ErrRoleInUse):
		writeError(w, http.StatusConflict, "role_in_use",
			"This role is still assigned to members.")
	case errors.Is(err, rbac.ErrBuiltinImmutable):
		writeError(w, http.StatusForbidden, "role_builtin",
			"Built-in roles cannot be deleted.")
	case errors.Is(err, rbac.ErrOwnerRoleLocked):
		writeError(w, http.StatusForbidden, "owner_role_locked",
			"The workspace owner role cannot be assigned or revoked.")
	case errors.Is(err, rbac.ErrLastOwnerLocked):
		writeError(w, http.StatusConflict, "last_owner_locked",
			"The last owner of a workspace cannot be removed or downgraded. "+
				"Grant the owner role to another member first.")
	case errors.Is(err, rbac.ErrInvalidRoleCode):
		writeError(w, http.StatusBadRequest, "validation_failed",
			"Role code must be 2-64 characters of lowercase letters, digits, '-' or '_'.")
	case errors.Is(err, rbac.ErrInvalidRoleName):
		writeError(w, http.StatusBadRequest, "validation_failed", "Role name is required.")
	case errors.Is(err, rbac.ErrInvalidScope):
		writeError(w, http.StatusBadRequest, "validation_failed", "Unsupported scope.")
	case errors.Is(err, rbac.ErrUnknownPermission):
		writeError(w, http.StatusBadRequest, "validation_failed",
			"Unknown permission code.")
	case errors.Is(err, rbac.ErrScopeMismatch):
		writeError(w, http.StatusBadRequest, "validation_failed",
			"Permission does not belong to the role scope.")
	default:
		logger.Error("rbac_internal_error", zap.Error(err))
		writeError(w, http.StatusInternalServerError, "internal_error",
			"Unexpected error. Please retry.")
	}
}
