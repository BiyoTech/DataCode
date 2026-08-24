// Package handler — Workspace and Git credential special handlers that
// require identity injection from request context.
//
// All pure-passthrough workspace/file/git-panel routes are now handled
// declaratively via GenericProxy / MakeValidatedProxy.
package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/middleware"
	"github.com/datacode/gateway/internal/rbac"
)

const workspacesBasePath = "/api/v1/workspaces"

// ---------------------------------------------------------------------------
// Workspace handlers — identity injection into request body
// ---------------------------------------------------------------------------

// CreateWorkspace proxies workspace creation to SQLMesh Service and records the
// owner role binding, which the gateway owns.
//
// The two steps are ordered so that a failure can never leave a workspace that
// nobody may open: if the binding cannot be stored, the freshly created
// workspace is deleted again and the caller receives an error.
func CreateWorkspace(
	cfg *config.Config, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())

		body, ok := identityBody(w, r, logger, true)
		if !ok {
			return
		}
		resp, err := callUpstream(r, http.MethodPost,
			cfg.Services.SQLMeshServiceURL, workspacesBasePath, body, logger)
		if err != nil {
			http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
			return
		}
		if !resp.OK() {
			writeUpstream(w, resp)
			return
		}

		var created struct {
			WorkspaceID string `json:"workspace_id"`
		}
		if err := json.Unmarshal(resp.Body, &created); err != nil || created.WorkspaceID == "" {
			logger.Error("workspace_create_response_unusable",
				zap.String("tenant_id", tenantID), zap.Error(err))
			http.Error(w, `{"error":"unexpected upstream response"}`, http.StatusBadGateway)
			return
		}

		if err := rbacService.GrantWorkspaceOwner(
			r.Context(), tenantID, created.WorkspaceID, userID,
		); err != nil {
			// Compensate: without the owner binding the workspace would be
			// invisible and unmanageable for everyone.
			logger.Error("workspace_owner_binding_failed",
				zap.String("workspace_id", created.WorkspaceID),
				zap.String("tenant_id", tenantID), zap.Error(err))
			compensateWorkspaceCreation(r, cfg, created.WorkspaceID, userID, tenantID, logger)
			http.Error(w, `{"error":"failed to record workspace ownership"}`, http.StatusBadGateway)
			return
		}

		writeUpstream(w, withWorkspaceCapabilities(resp, rbac.RoleWorkspaceOwner,
			ownerPermissions(r, rbacService, tenantID, logger), logger))
	}
}

// compensateWorkspaceCreation deletes a workspace whose ownership could not be
// recorded, so no orphan is left behind.
func compensateWorkspaceCreation(
	r *http.Request, cfg *config.Config, workspaceID, userID, tenantID string, logger *zap.Logger,
) {
	path := fmt.Sprintf("%s/%s?user_id=%s&tenant_id=%s",
		workspacesBasePath, url.PathEscape(workspaceID),
		url.QueryEscape(userID), url.QueryEscape(tenantID))
	// This is an internal rollback issued by the gateway itself, not a client
	// action, so it carries the delete capability explicitly.
	r.Header.Set(middleware.HeaderPermissions, rbac.PermWorkspaceDelete)
	if _, err := callUpstream(r, http.MethodDelete,
		cfg.Services.SQLMeshServiceURL, path, nil, logger); err != nil {
		logger.Error("workspace_create_compensation_failed",
			zap.String("workspace_id", workspaceID), zap.Error(err))
		return
	}
	logger.Warn("workspace_create_compensated", zap.String("workspace_id", workspaceID))
}

// ownerPermissions resolves the effective permissions of the workspace owner
// role; an error only degrades the response payload, never the request.
func ownerPermissions(
	r *http.Request, rbacService *rbac.Service, tenantID string, logger *zap.Logger,
) []string {
	permissions, err := rbacService.EffectivePermissions(
		r.Context(), tenantID, rbac.ScopeWorkspace, rbac.RoleWorkspaceOwner)
	if err != nil {
		logger.Warn("workspace_owner_permissions_unavailable", zap.Error(err))
		return []string{}
	}
	return permissions
}

// withWorkspaceCapabilities injects the authoritative role and permission set
// into a single-workspace upstream payload.
func withWorkspaceCapabilities(
	resp *upstreamResponse, roleCode string, permissions []string, logger *zap.Logger,
) *upstreamResponse {
	var payload map[string]interface{}
	if err := json.Unmarshal(resp.Body, &payload); err != nil {
		logger.Warn("workspace_payload_not_an_object", zap.Error(err))
		return resp
	}
	payload["role"] = roleCode
	payload["is_owner"] = roleCode == rbac.RoleWorkspaceOwner
	payload["permissions"] = permissions

	encoded, err := json.Marshal(payload)
	if err != nil {
		return resp
	}
	resp.Body = encoded
	return resp
}

// BindGitRepo proxies Git repository binding to SQLMesh Service.
func BindGitRepo(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectIdentityIntoBody(cfg, logger, workspacesBasePath+"/bind-git", true)
}

// SyncWorkspace proxies "commit + push my working copy" to SQLMesh Service.
func SyncWorkspace(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectIdentityIntoBody(cfg, logger, workspacesBasePath+"/sync", true)
}

// CreateGitCredential proxies Git credential creation to SQLMesh Service.
// Credentials are user-scoped, so no tenant is injected.
func CreateGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectIdentityIntoBody(cfg, logger, "/api/v1/git/credentials", false)
}

// OpenWorkspace materializes the caller's working copy of a workspace.
// The workspace ID comes from the URL, the identity from the verified token.
func OpenWorkspace(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		workspaceID := chi.URLParam(r, "workspaceID")
		body, ok := identityBody(w, r, logger, true)
		if !ok {
			return
		}
		backendPath := fmt.Sprintf("%s/%s/open", workspacesBasePath, url.PathEscape(workspaceID))
		proxyPost(w, r, cfg.Services.SQLMeshServiceURL, backendPath, body, logger)
	}
}

// injectIdentityIntoBody is a shared factory for handlers that read a JSON body,
// overwrite the identity fields with the authenticated values, and POST to the
// given backend path.
//
// Identity is always overwritten: a client-supplied user_id / tenant_id would
// otherwise let a caller act on behalf of another account or tenant.
func injectIdentityIntoBody(
	cfg *config.Config, logger *zap.Logger, backendPath string, withTenant bool,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, ok := identityBody(w, r, logger, withTenant)
		if !ok {
			return
		}
		proxyPost(w, r, cfg.Services.SQLMeshServiceURL, backendPath, body, logger)
	}
}

// identityBody parses the JSON body and stamps the verified identity onto it.
// Returns false when the body is unusable (a response has already been written).
func identityBody(
	w http.ResponseWriter, r *http.Request, logger *zap.Logger, withTenant bool,
) ([]byte, bool) {
	userID := middleware.GetUserID(r.Context())
	tenantID := middleware.GetTenantID(r.Context())

	raw, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
		return nil, false
	}
	defer r.Body.Close()

	reqBody := map[string]interface{}{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &reqBody); err != nil {
			http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
			return nil, false
		}
	}

	stampIdentityField(reqBody, "user_id", userID, r.URL.Path, logger)
	if withTenant {
		if tenantID == "" {
			middleware.WriteForbidden(w, middleware.DenyNoTenant, "")
			return nil, false
		}
		stampIdentityField(reqBody, "tenant_id", tenantID, r.URL.Path, logger)
	}

	body, err := json.Marshal(reqBody)
	if err != nil {
		http.Error(w, `{"error":"failed to encode request body"}`, http.StatusInternalServerError)
		return nil, false
	}
	return body, true
}

// stampIdentityField overwrites one identity field, warning on forged values.
func stampIdentityField(
	body map[string]interface{}, field, verified, path string, logger *zap.Logger,
) {
	if spoofed, ok := body[field].(string); ok && spoofed != verified {
		logger.Warn("request_identity_overridden",
			zap.String("path", path),
			zap.String("field", field))
	}
	body[field] = verified
}

// ---------------------------------------------------------------------------
// Workspace / credential listing — identity from the verified token
// ---------------------------------------------------------------------------

// ListWorkspaces returns the workspaces the caller has been granted access to.
//
// The gateway owns the role bindings, so it resolves them first and asks the
// SQLMesh Service only for the metadata of those identifiers. The role and the
// effective permissions in the response are authoritative and always come from
// the gateway, never from the downstream service.
func ListWorkspaces(
	cfg *config.Config, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		if tenantID == "" {
			middleware.WriteForbidden(w, middleware.DenyNoTenant, "")
			return
		}

		bindings, err := rbacService.ListWorkspaceRolesForUser(r.Context(), tenantID, userID)
		if err != nil {
			logger.Error("workspace_bindings_lookup_failed",
				zap.String("tenant_id", tenantID), zap.Error(err))
			writeError(w, http.StatusInternalServerError, "internal_error",
				"Failed to resolve workspace access.")
			return
		}
		if len(bindings) == 0 {
			// No membership: skip the upstream call entirely.
			writeJSON(w, http.StatusOK, map[string]interface{}{
				"workspaces": []interface{}{},
			})
			return
		}

		ids := make([]string, 0, len(bindings))
		for workspaceID := range bindings {
			ids = append(ids, workspaceID)
		}
		sort.Strings(ids)

		query, ok := identityQuery(w, r)
		if !ok {
			return
		}
		path := fmt.Sprintf("%s?%s&workspace_ids=%s",
			workspacesBasePath, query, url.QueryEscape(strings.Join(ids, ",")))

		resp, err := callUpstream(r, http.MethodGet, cfg.Services.SQLMeshServiceURL, path, nil, logger)
		if err != nil {
			http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
			return
		}
		if !resp.OK() {
			writeUpstream(w, resp)
			return
		}

		var payload struct {
			Workspaces []map[string]interface{} `json:"workspaces"`
		}
		if err := json.Unmarshal(resp.Body, &payload); err != nil {
			logger.Error("workspace_list_response_unusable", zap.Error(err))
			http.Error(w, `{"error":"unexpected upstream response"}`, http.StatusBadGateway)
			return
		}

		permissionsByRole := make(map[string][]string, 4)
		enriched := make([]map[string]interface{}, 0, len(payload.Workspaces))
		for _, workspace := range payload.Workspaces {
			workspaceID, _ := workspace["workspace_id"].(string)
			roleCode, granted := bindings[workspaceID]
			if !granted {
				// Defence in depth: never expose a workspace the caller has no
				// binding for, whatever the downstream service returned.
				continue
			}
			permissions, cached := permissionsByRole[roleCode]
			if !cached {
				permissions, err = rbacService.EffectivePermissions(
					r.Context(), tenantID, rbac.ScopeWorkspace, roleCode)
				if err != nil {
					logger.Warn("workspace_permissions_unavailable",
						zap.String("role_code", roleCode), zap.Error(err))
					permissions = []string{}
				}
				permissionsByRole[roleCode] = permissions
			}
			workspace["role"] = roleCode
			workspace["is_owner"] = roleCode == rbac.RoleWorkspaceOwner
			workspace["permissions"] = permissions
			enriched = append(enriched, workspace)
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{"workspaces": enriched})
	}
}

// ListWorkspaceRemoteBranches proxies remote branch discovery for a workspace.
func ListWorkspaceRemoteBranches(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		query, ok := identityQuery(w, r)
		if !ok {
			return
		}
		backendPath := fmt.Sprintf("%s/%s/remote-branches?%s",
			workspacesBasePath, url.PathEscape(chi.URLParam(r, "workspaceID")), query)
		proxyGet(w, r, cfg.Services.SQLMeshServiceURL, backendPath, logger)
	}
}

// ListWorkspaceMembers returns the role bindings of a workspace, enriched with
// the usernames owned by the auth domain. Authorization was already enforced by
// the Authorize middleware (workspace:member:read).
func ListWorkspaceMembers(
	authService *auth.Service, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")

		bindings, err := rbacService.ListWorkspaceMembers(r.Context(), tenantID, workspaceID)
		if err != nil {
			logger.Error("workspace_members_lookup_failed",
				zap.String("workspace_id", workspaceID), zap.Error(err))
			writeError(w, http.StatusInternalServerError, "internal_error",
				"Failed to load workspace members.")
			return
		}

		usernames, err := tenantUsernames(r, authService, tenantID)
		if err != nil {
			logger.Error("tenant_member_lookup_failed",
				zap.String("tenant_id", tenantID), zap.Error(err))
			writeError(w, http.StatusBadGateway, "upstream_error",
				"Failed to resolve member names.")
			return
		}

		members := make([]map[string]interface{}, 0, len(bindings))
		for _, binding := range bindings {
			members = append(members, map[string]interface{}{
				"userId":   binding.UserID,
				"username": usernames[binding.UserID],
				"role":     binding.RoleCode,
				"isOwner":  binding.RoleCode == rbac.RoleWorkspaceOwner,
				"joinedAt": binding.CreatedAt,
			})
		}
		writeJSON(w, http.StatusOK, map[string]interface{}{"members": members})
	}
}

// AddWorkspaceMember grants a tenant member access to a workspace.
//
// Both facts it depends on are owned by the gateway: tenant membership of the
// target account and the workspace role binding itself.
func AddWorkspaceMember(
	authService *auth.Service, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actorID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")

		var req struct {
			TargetUserID string `json:"target_user_id"`
			Role         string `json:"role"`
		}
		if !decodeJSON(w, r, &req) {
			return
		}
		if req.TargetUserID == "" {
			writeError(w, http.StatusBadRequest, "validation_failed",
				"target_user_id is required.")
			return
		}
		if req.Role == "" {
			req.Role = rbac.RoleWorkspaceViewer
		}

		inTenant, err := authService.HasMember(r.Context(), tenantID, req.TargetUserID)
		if err != nil {
			logger.Error("tenant_member_lookup_failed",
				zap.String("tenant_id", tenantID), zap.Error(err))
			writeError(w, http.StatusBadGateway, "upstream_error",
				"Failed to verify tenant membership.")
			return
		}
		if !inTenant {
			logger.Warn("workspace_member_add_rejected_foreign_user",
				zap.String("workspace_id", workspaceID),
				zap.String("tenant_id", tenantID))
			writeError(w, http.StatusBadRequest, "validation_failed",
				"Target user is not a member of this tenant.")
			return
		}

		if err := rbacService.GrantWorkspaceMember(
			r.Context(), actorID, tenantID, workspaceID, req.TargetUserID, req.Role,
		); err != nil {
			writeRBACError(w, logger, err)
			return
		}
		writeJSON(w, http.StatusCreated, map[string]interface{}{
			"success":     true,
			"workspaceId": workspaceID,
			"userId":      req.TargetUserID,
			"role":        req.Role,
		})
	}
}

// RemoveWorkspaceMember revokes workspace access and asks the SQLMesh Service
// to drop the member's local working copy. The binding is removed first: a
// failed cleanup must never leave the access in place.
func RemoveWorkspaceMember(
	cfg *config.Config, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actorID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")
		memberUserID := chi.URLParam(r, "memberUserID")

		if err := rbacService.RevokeWorkspaceMember(
			r.Context(), actorID, tenantID, workspaceID, memberUserID,
		); err != nil {
			writeRBACError(w, logger, err)
			return
		}

		// Best effort: the access is already gone, the checkout is disposable.
		path := fmt.Sprintf("%s/%s/members/%s?user_id=%s&tenant_id=%s",
			workspacesBasePath, url.PathEscape(workspaceID), url.PathEscape(memberUserID),
			url.QueryEscape(actorID), url.QueryEscape(tenantID))
		if _, err := callUpstream(r, http.MethodDelete,
			cfg.Services.SQLMeshServiceURL, path, nil, logger); err != nil {
			logger.Warn("workspace_member_cleanup_failed",
				zap.String("workspace_id", workspaceID),
				zap.String("user_id", memberUserID), zap.Error(err))
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
	}
}

// DeleteWorkspace proxies workspace deletion and then drops every role binding
// of the deleted workspace.
func DeleteWorkspace(
	cfg *config.Config, rbacService *rbac.Service, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actorID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")

		query, ok := identityQuery(w, r)
		if !ok {
			return
		}
		if r.URL.Query().Get("delete_remote") == "true" {
			query += "&delete_remote=true"
		}
		path := fmt.Sprintf("%s/%s?%s", workspacesBasePath, url.PathEscape(workspaceID), query)

		resp, err := callUpstream(r, http.MethodDelete,
			cfg.Services.SQLMeshServiceURL, path, nil, logger)
		if err != nil {
			http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
			return
		}
		if resp.OK() {
			if err := rbacService.PurgeWorkspace(
				r.Context(), actorID, tenantID, workspaceID,
			); err != nil {
				// The workspace is gone; a stale binding only hides a dead
				// entry from nobody, so the request still succeeds.
				logger.Error("workspace_bindings_purge_failed",
					zap.String("workspace_id", workspaceID), zap.Error(err))
			}
		}
		writeUpstream(w, resp)
	}
}

// tenantUsernames maps user ids to usernames inside a tenant.
func tenantUsernames(
	r *http.Request, authService *auth.Service, tenantID string,
) (map[string]string, error) {
	members, err := authService.ListMembers(r.Context(), tenantID)
	if err != nil {
		return nil, err
	}
	usernames := make(map[string]string, len(members))
	for _, member := range members {
		usernames[member.UserID] = member.Username
	}
	return usernames, nil
}

// identityQuery builds the `user_id=..&tenant_id=..` query string from the
// verified token. Requests without an active tenant are rejected.
func identityQuery(w http.ResponseWriter, r *http.Request) (string, bool) {
	userID := middleware.GetUserID(r.Context())
	tenantID := middleware.GetTenantID(r.Context())
	if tenantID == "" {
		middleware.WriteForbidden(w, middleware.DenyNoTenant, "")
		return "", false
	}
	return fmt.Sprintf("user_id=%s&tenant_id=%s",
		url.QueryEscape(userID), url.QueryEscape(tenantID)), true
}

// ListGitCredentials proxies Git credentials listing to SQLMesh Service.
// Uses user_id from request context.
func ListGitCredentials(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials?user_id=%s", url.QueryEscape(userID))
		proxyGet(w, r, cfg.Services.SQLMeshServiceURL, targetURL, logger)
	}
}

// ---------------------------------------------------------------------------
// Credential management — user_id + path params
// ---------------------------------------------------------------------------

// DeleteGitCredential proxies Git credential deletion to SQLMesh Service.
// Appends user_id from context as a query param for authorization.
func DeleteGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		credentialID := chi.URLParam(r, "credentialID")
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials/%s?user_id=%s",
			url.PathEscape(credentialID), url.QueryEscape(userID))
		proxyDelete(w, r, cfg.Services.SQLMeshServiceURL, targetURL, logger)
	}
}

// SetDefaultGitCredential proxies setting a credential as default to SQLMesh Service.
// Appends user_id from context as a query param for authorization.
func SetDefaultGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		credentialID := chi.URLParam(r, "credentialID")
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials/%s/default?user_id=%s",
			url.PathEscape(credentialID), url.QueryEscape(userID))
		proxyPatch(w, r, cfg.Services.SQLMeshServiceURL, targetURL, nil, logger)
	}
}
