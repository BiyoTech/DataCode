// Package handler — Workspace and Git credential special handlers that
// require identity injection from request context.
//
// All pure-passthrough workspace/file/git-panel routes are now handled
// declaratively via GenericProxy / MakeValidatedProxy.
//
// P5.5 NOTE — where authorization data comes from:
//
// These handlers used to read and write the gateway's own `workspace_members`
// table. They no longer do: identity-service is the single writer of that
// dataset (design 06 P2, and a hard prerequisite for the P7 Global/Cell split,
// which cannot move a schema another service still writes).
//
//   - READS of the CALLER's own bindings come from the authorization snapshot
//     the Authorize middleware already fetched — no extra round trip.
//   - READS of OTHER users' bindings and ALL WRITES go through
//     authz.Bindings, identity-service's internal binding API.
package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/authz"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/middleware"
	"github.com/datacode/shared/httperr"
	rbac "github.com/datacode/shared/rbac"
)

const workspacesBasePath = "/api/v1/workspaces"

// controlPlaneURL returns the upstream for workspace-metadata and
// Git-credential operations: control-service since P5. There is no fallback:
// the legacy per-service entries were removed from platform.yaml with the
// services they named, so an unset services.control surfaces as proxy errors
// rather than traffic silently routed elsewhere.
func controlPlaneURL(cfg *config.Config) string {
	return cfg.Services.ControlServiceURL
}

// ---------------------------------------------------------------------------
// Workspace handlers — identity injection into request body
// ---------------------------------------------------------------------------

// CreateWorkspace proxies workspace creation to the control plane and records
// the owner role binding in identity-service.
//
// The two steps are ordered so that a failure can never leave a workspace that
// nobody may open: if the binding cannot be stored, the freshly created
// workspace is deleted again and the caller receives an error.
//
// The creator's cached snapshot MUST be dropped afterwards. It was taken before
// the workspace existed, so it carries no binding for it — and the very next
// request the client makes is "open the workspace I just created", a
// workspace-scoped route. Decided from the stale snapshot it is denied as
// workspace_not_member: the creator is told they are not a member of their own
// brand-new workspace, for as long as the cache TTL lasts.
func CreateWorkspace(
	cfg *config.Config, bindings *authz.Bindings, resolver *authz.Resolver,
	logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())

		body, ok := identityBody(w, r, logger, true)
		if !ok {
			return
		}
		resp, err := callUpstream(r, http.MethodPost,
			controlPlaneURL(cfg), workspacesBasePath, body, logger)
		if err != nil {
			httperr.WriteUpstreamUnavailable(w)
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

		if err := bindings.GrantWorkspaceOwner(
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
		// The ownership binding must apply to the creator's next request, which
		// is the open that follows immediately (see the note above).
		resolver.Invalidate(userID, tenantID)

		writeUpstream(w, withWorkspaceCapabilities(resp, rbac.RoleWorkspaceOwner,
			ownerPermissions(r, bindings, tenantID, logger), logger))
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
		controlPlaneURL(cfg), path, nil, logger); err != nil {
		logger.Error("workspace_create_compensation_failed",
			zap.String("workspace_id", workspaceID), zap.Error(err))
		return
	}
	logger.Warn("workspace_create_compensated", zap.String("workspace_id", workspaceID))
}

// ownerPermissions resolves the effective permissions of the workspace owner
// role; an error only degrades the response payload, never the request.
func ownerPermissions(
	r *http.Request, bindings *authz.Bindings, tenantID string, logger *zap.Logger,
) []string {
	permissions, err := bindings.RolePermissions(
		r.Context(), tenantID, string(rbac.ScopeWorkspace), rbac.RoleWorkspaceOwner)
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

// BindGitRepo proxies Git repository binding to the control plane.
func BindGitRepo(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectIdentityIntoBody(cfg, logger, workspacesBasePath+"/bind-git", true)
}

// SyncWorkspace proxies "commit + push my working copy" to the control plane.
func SyncWorkspace(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectIdentityIntoBody(cfg, logger, workspacesBasePath+"/sync", true)
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
		proxyPost(w, r, controlPlaneURL(cfg), backendPath, body, logger)
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
		proxyPost(w, r, controlPlaneURL(cfg), backendPath, body, logger)
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
// The caller's bindings and the permissions of the roles they hold are read
// from the authorization snapshot the Authorize middleware already resolved:
// it carries exactly this data, so the listing needs no call to
// identity-service at all. The control plane is asked only for the METADATA of
// those identifiers, and the role/permissions in the response always come from
// the snapshot — never from the downstream service.
func ListWorkspaces(
	cfg *config.Config, resolver *authz.Resolver, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		if tenantID == "" {
			middleware.WriteForbidden(w, middleware.DenyNoTenant, "")
			return
		}

		snapshot, err := resolver.Resolve(
			r.Context(), userID, tenantID, middleware.GetPermsVersion(r.Context()))
		if err != nil {
			logger.Error("workspace_bindings_lookup_failed",
				zap.String("tenant_id", tenantID), zap.Error(err))
			writeError(w, http.StatusServiceUnavailable, "authz_unavailable",
				"Authorization data is temporarily unavailable.")
			return
		}
		bindings := snapshot.WorkspaceRoles
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

		resp, err := callUpstream(r, http.MethodGet, controlPlaneURL(cfg), path, nil, logger)
		if err != nil {
			httperr.WriteUpstreamUnavailable(w)
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

		enriched := make([]map[string]interface{}, 0, len(payload.Workspaces))
		for _, workspace := range payload.Workspaces {
			workspaceID, _ := workspace["workspace_id"].(string)
			roleCode, granted := bindings[workspaceID]
			if !granted {
				// Defence in depth: never expose a workspace the caller has no
				// binding for, whatever the downstream service returned.
				continue
			}
			permissions := snapshot.PermissionsForWorkspaceRole(roleCode)
			if permissions == nil {
				permissions = []string{}
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
		proxyGet(w, r, controlPlaneURL(cfg), backendPath, logger)
	}
}

// ListWorkspaceMembers returns the role bindings of a workspace, enriched with
// the usernames owned by the identity domain. Authorization was already
// enforced by the Authorize middleware (workspace:member:read).
//
// Both facts come from identity-service: these are OTHER users' bindings, which
// the caller's own snapshot does not contain.
func ListWorkspaceMembers(
	bindingsClient *authz.Bindings, logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")

		bindings, err := bindingsClient.ListWorkspaceMembers(r.Context(), tenantID, workspaceID)
		if err != nil {
			logger.Error("workspace_members_lookup_failed",
				zap.String("workspace_id", workspaceID), zap.Error(err))
			writeBindingError(w, logger, err)
			return
		}

		usernames, err := tenantUsernames(r, bindingsClient, tenantID)
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
// The tenant-membership check of the target account and the binding write are
// both performed by identity-service, which owns those tables; the gateway has
// already authorized the ACTOR through the Authorize middleware.
func AddWorkspaceMember(
	bindingsClient *authz.Bindings, resolver *authz.Resolver, logger *zap.Logger,
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

		if err := bindingsClient.GrantWorkspaceMember(
			r.Context(), actorID, tenantID, workspaceID, req.TargetUserID, req.Role,
		); err != nil {
			writeBindingError(w, logger, err)
			return
		}
		// The target's cached snapshot no longer reflects their access, so drop
		// it: the new binding must apply to their very next request.
		resolver.Invalidate(req.TargetUserID, tenantID)

		writeJSON(w, http.StatusCreated, map[string]interface{}{
			"success":     true,
			"workspaceId": workspaceID,
			"userId":      req.TargetUserID,
			"role":        req.Role,
		})
	}
}

// RemoveWorkspaceMember revokes workspace access and asks the control plane to
// drop the member's local working copy. The binding is removed first: a failed
// cleanup must never leave the access in place.
func RemoveWorkspaceMember(
	cfg *config.Config, bindingsClient *authz.Bindings, resolver *authz.Resolver,
	logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actorID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")
		memberUserID := chi.URLParam(r, "memberUserID")

		if err := bindingsClient.RevokeWorkspaceMember(
			r.Context(), actorID, tenantID, workspaceID, memberUserID,
		); err != nil {
			writeBindingError(w, logger, err)
			return
		}
		// Revocation must take effect immediately, not after the cache TTL.
		resolver.Invalidate(memberUserID, tenantID)

		// Best effort: the access is already gone, the checkout is disposable.
		path := fmt.Sprintf("%s/%s/members/%s?user_id=%s&tenant_id=%s",
			workspacesBasePath, url.PathEscape(workspaceID), url.PathEscape(memberUserID),
			url.QueryEscape(actorID), url.QueryEscape(tenantID))
		if _, err := callUpstream(r, http.MethodDelete,
			controlPlaneURL(cfg), path, nil, logger); err != nil {
			logger.Warn("workspace_member_cleanup_failed",
				zap.String("workspace_id", workspaceID),
				zap.String("user_id", memberUserID), zap.Error(err))
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
	}
}

// DeleteWorkspace proxies workspace deletion and then drops every role binding
// of the deleted workspace.
//
// The caller's query string is deliberately NOT forwarded beyond the identity
// parameters: deletion takes no options. In particular there is no
// delete_remote passthrough anymore — the remote Git repository is always
// preserved, so accepting a parameter that the control plane ignores would only
// suggest otherwise.
func DeleteWorkspace(
	cfg *config.Config, bindingsClient *authz.Bindings, resolver *authz.Resolver,
	logger *zap.Logger,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		actorID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())
		workspaceID := chi.URLParam(r, "workspaceID")

		query, ok := identityQuery(w, r)
		if !ok {
			return
		}
		path := fmt.Sprintf("%s/%s?%s", workspacesBasePath, url.PathEscape(workspaceID), query)

		resp, err := callUpstream(r, http.MethodDelete,
			controlPlaneURL(cfg), path, nil, logger)
		if err != nil {
			httperr.WriteUpstreamUnavailable(w)
			return
		}
		if resp.OK() {
			if err := bindingsClient.PurgeWorkspace(
				r.Context(), actorID, tenantID, workspaceID,
			); err != nil {
				// The workspace is gone; a stale binding only hides a dead
				// entry from nobody, so the request still succeeds.
				logger.Error("workspace_bindings_purge_failed",
					zap.String("workspace_id", workspaceID), zap.Error(err))
			}
			// Every member of the deleted workspace holds a stale snapshot.
			resolver.InvalidateTenant(tenantID)
		}
		writeUpstream(w, resp)
	}
}

// tenantUsernames maps user ids to usernames inside a tenant.
func tenantUsernames(
	r *http.Request, bindingsClient *authz.Bindings, tenantID string,
) (map[string]string, error) {
	members, err := bindingsClient.ListTenantMembers(r.Context(), tenantID)
	if err != nil {
		return nil, err
	}
	usernames := make(map[string]string, len(members))
	for _, member := range members {
		usernames[member.UserID] = member.Username
	}
	return usernames, nil
}

// writeBindingError maps identity-service binding failures onto HTTP codes,
// keeping the user-facing wording of the pre-P5.5 local implementation.
func writeBindingError(w http.ResponseWriter, logger *zap.Logger, err error) {
	switch {
	case errors.Is(err, authz.ErrBindingNotFound):
		writeError(w, http.StatusNotFound, "member_not_found",
			"This member has no access to the workspace.")
	case errors.Is(err, authz.ErrRoleNotFound):
		writeError(w, http.StatusNotFound, "role_not_found", "Role not found.")
	case errors.Is(err, authz.ErrLastOwnerLocked):
		writeError(w, http.StatusConflict, "last_owner_locked",
			"The last owner of a workspace cannot be removed or downgraded.")
	case errors.Is(err, authz.ErrNotTenantMember):
		writeError(w, http.StatusBadRequest, "validation_failed",
			"Target user is not a member of this tenant.")
	case errors.Is(err, authz.ErrBindingsUnavailable):
		writeError(w, http.StatusBadGateway, "identity_unavailable",
			"Identity service is unavailable. Please retry.")
	default:
		logger.Error("workspace_binding_error", zap.Error(err))
		writeError(w, http.StatusInternalServerError, "internal_error",
			"Unexpected error. Please retry.")
	}
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

// Git credentials have no handler here on purpose. They are user-scoped control
// plane data and the control plane reads the owner from the identity headers, so
// the four hand-written handlers that used to append "?user_id=..." were passing
// a parameter the endpoints do not even accept — while SanitizeIdentity already
// guarantees no client-supplied owner survives. The whole group is a transparent
// proxy in the router.
