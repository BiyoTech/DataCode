package api

import (
	"errors"
	"net"
	"net/http"
	"strings"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/auth"
	"github.com/datacode/identity-service/internal/rbac"
	"github.com/datacode/shared/httperr"
)

// AuthHandler exposes the authentication endpoints owned by the gateway.
type AuthHandler struct {
	service *auth.Service
	rbac    *rbac.Service
	logger  *zap.Logger
}

// NewAuthHandler builds the authentication handler set. The RBAC service is
// required because tenant roles are database-defined: their validity and the
// permissions they grant can only be answered by the authorization domain.
func NewAuthHandler(service *auth.Service, rbacService *rbac.Service, logger *zap.Logger) *AuthHandler {
	return &AuthHandler{service: service, rbac: rbacService, logger: logger}
}

// ---------------------------------------------------------------------------
// Request payloads
// ---------------------------------------------------------------------------

type registerRequest struct {
	Username   string `json:"username"`
	Password   string `json:"password"`
	TenantName string `json:"tenantName"`
}

type loginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type selectTenantRequest struct {
	// PreAuthToken is supplied right after login; in-app identity switching
	// instead relies on the Authorization header.
	PreAuthToken string `json:"preAuthToken"`
	TenantID     string `json:"tenantId"`
}

type refreshRequest struct {
	RefreshToken string `json:"refreshToken"`
}

type changePasswordRequest struct {
	CurrentPassword string `json:"currentPassword"`
	NewPassword     string `json:"newPassword"`
}

type addMemberRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
	Role     string `json:"role"`
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

// Register creates a new tenant with its owning administrator.
func (h *AuthHandler) Register(w http.ResponseWriter, r *http.Request) {
	var req registerRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	result, err := h.service.Register(r.Context(), req.Username, req.Password, req.TenantName)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	// A brand-new tenant owns no roles yet; instantiate the built-in set so its
	// administrator can manage members and roles right away.
	if err := h.rbac.EnsureTenantRoles(r.Context(), result.User.TenantID); err != nil {
		h.logger.Error("rbac_tenant_roles_instantiation_failed",
			zap.String("tenant_id", result.User.TenantID), zap.Error(err))
		writeError(w, http.StatusInternalServerError, "internal_error",
			"Tenant created but its roles could not be initialized.")
		return
	}
	writeJSON(w, http.StatusCreated, result)
}

// Login performs phase one of authentication.
func (h *AuthHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req loginRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	result, err := h.service.Login(r.Context(), req.Username, req.Password, clientIP(r))
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// SelectTenant completes authentication for a chosen tenant identity. It
// accepts either a pre-auth token (post-login) or a valid access token
// (in-app identity switch).
func (h *AuthHandler) SelectTenant(w http.ResponseWriter, r *http.Request) {
	var req selectTenantRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if strings.TrimSpace(req.TenantID) == "" {
		writeError(w, http.StatusBadRequest, "validation_failed", "tenantId is required.")
		return
	}

	userID, err := h.resolveSelectionSubject(r, req.PreAuthToken)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}

	result, err := h.service.SelectTenant(r.Context(), userID, req.TenantID)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// resolveSelectionSubject determines which user is selecting an identity.
// A pre-auth token takes precedence; otherwise a valid access token is used.
func (h *AuthHandler) resolveSelectionSubject(r *http.Request, preAuthToken string) (string, error) {
	issuer := h.service.Issuer()

	if strings.TrimSpace(preAuthToken) != "" {
		claims, err := issuer.Verify(preAuthToken, auth.TokenTypePreAuth)
		if err != nil {
			return "", err
		}
		return claims.Subject, nil
	}

	header := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if len(header) > len(prefix) && strings.EqualFold(header[:len(prefix)], prefix) {
		claims, err := issuer.Verify(strings.TrimSpace(header[len(prefix):]), auth.TokenTypeAccess)
		if err != nil {
			return "", err
		}
		return claims.Subject, nil
	}
	return "", auth.ErrInvalidToken
}

// Refresh rotates the refresh token and issues a new access token.
func (h *AuthHandler) Refresh(w http.ResponseWriter, r *http.Request) {
	var req refreshRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	result, err := h.service.Refresh(r.Context(), req.RefreshToken)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// Logout revokes the presented refresh token.
func (h *AuthHandler) Logout(w http.ResponseWriter, r *http.Request) {
	var req refreshRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if err := h.service.Logout(r.Context(), req.RefreshToken); err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"success": true})
}

// ChangePassword updates the caller's password and drops all its sessions.
func (h *AuthHandler) ChangePassword(w http.ResponseWriter, r *http.Request) {
	var req changePasswordRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	userID := GetUserID(r.Context())
	if err := h.service.ChangePassword(
		r.Context(), userID, req.CurrentPassword, req.NewPassword,
	); err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"success": true})
}

// ListMemberships returns every tenant identity of the caller, used by the
// in-app identity switcher.
func (h *AuthHandler) ListMemberships(w http.ResponseWriter, r *http.Request) {
	memberships, err := h.service.ListMemberships(r.Context(), GetUserID(r.Context()))
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"memberships": memberships})
}

// ListMembers returns members of the caller's active tenant (admin only).
func (h *AuthHandler) ListMembers(w http.ResponseWriter, r *http.Request) {
	members, err := h.service.ListMembers(r.Context(), GetTenantID(r.Context()))
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"members": members})
}

// ListTenantMemberCandidates returns the minimal directory of the caller's
// active tenant, used by workspace owners to pick who may access a workspace.
//
// Reachable by any authenticated tenant member (not admin-only) because
// granting workspace access is a workspace-owner action, not a tenant-admin
// one. Only userId/username are exposed — tenant roles and join dates are
// administrative data that this use case does not need.
//
// Accounts whose tenant role does not grant tenant:workspace:join are filtered
// out, so a read-only tenant member can never be added to a workspace by
// mistake.
func (h *AuthHandler) ListTenantMemberCandidates(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r.Context())
	members, err := h.service.ListMembers(r.Context(), tenantID)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}

	joinable := make(map[string]bool, 4)
	candidates := make([]map[string]string, 0, len(members))
	for _, m := range members {
		allowed, cached := joinable[m.Role]
		if !cached {
			allowed, err = h.rbac.Can(r.Context(), tenantID,
				rbac.ScopeTenant, m.Role, rbac.PermTenantWorkspaceJoin)
			if err != nil {
				writeRBACError(w, h.logger, err)
				return
			}
			joinable[m.Role] = allowed
		}
		if !allowed {
			continue
		}
		candidates = append(candidates, map[string]string{
			"userId":   m.UserID,
			"username": m.Username,
		})
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"candidates": candidates})
}

// AddMember adds a sub account to the caller's active tenant. The role must be
// one of the tenant's own tenant-scoped roles, including custom ones.
func (h *AuthHandler) AddMember(w http.ResponseWriter, r *http.Request) {
	var req addMemberRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	// Tenant scope always comes from the verified token, never from the body.
	tenantID := GetTenantID(r.Context())
	if err := h.rbac.ValidateAssignable(
		r.Context(), tenantID, rbac.ScopeTenant, req.Role,
	); err != nil {
		writeRBACError(w, h.logger, err)
		return
	}

	result, err := h.service.AddMember(r.Context(), tenantID, req.Username, req.Password, req.Role)
	if err != nil {
		h.writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, result)
}

// CurrentUser returns the verified principal of the active identity together
// with the capabilities of its tenant role, which the frontend uses to gate the
// UI. The gateway stays the only authority: the UI merely hides what it is told.
func (h *AuthHandler) CurrentUser(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	memberships, err := h.service.ListMemberships(ctx, GetUserID(ctx))
	if err != nil {
		h.writeServiceError(w, err)
		return
	}

	tenantID := GetTenantID(ctx)
	tenantName := ""
	for _, m := range memberships {
		if m.TenantID == tenantID {
			tenantName = m.TenantName
			break
		}
	}

	role := GetRole(ctx)
	permissions, err := h.rbac.EffectivePermissions(ctx, tenantID, rbac.ScopeTenant, role)
	if err != nil {
		writeRBACError(w, h.logger, err)
		return
	}
	roleName := role
	if definition, err := h.rbac.RoleByCode(ctx, tenantID, rbac.ScopeTenant, role); err == nil {
		roleName = definition.Name
	}

	// Display preferences travel with /me so the frontend can pick the render
	// timezone on its first call instead of guessing local time and correcting
	// it a request later.
	prefs, prefErr := h.service.GetPreferences(ctx, GetUserID(ctx))
	if prefErr != nil {
		// Non-fatal: fall back to the platform default rather than failing the
		// whole identity payload over a display setting.
		h.logger.Warn("preferences_read_failed", zap.Error(prefErr))
		prefs = auth.Preferences{Timezone: "UTC", Locale: "en"}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"userId":          GetUserID(ctx),
		"username":        GetUsername(ctx),
		"tenantId":        tenantID,
		"tenantName":      tenantName,
		"role":            role,
		"roleName":        roleName,
		"permissions":     permissions,
		"membershipCount": len(memberships),
		"timezone":        prefs.Timezone,
		"locale":          prefs.Locale,
	})
}

// UpdatePreferences stores the caller's timezone and UI language.
//
// PATCH /me/preferences  {"timezone": "Asia/Shanghai", "locale": "zh"}
//
// The timezone is validated against the runtime's zone database, so an
// unknown name is rejected here rather than breaking every later render.
func (h *AuthHandler) UpdatePreferences(w http.ResponseWriter, r *http.Request) {
	var prefs auth.Preferences
	if !decodeJSON(w, r, &prefs) {
		return
	}

	updated, err := h.service.UpdatePreferences(r.Context(), GetUserID(r.Context()), prefs)
	if err != nil {
		writeAuthError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// writeServiceError maps domain errors to HTTP status codes without leaking
// internal details.
func (h *AuthHandler) writeServiceError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, auth.ErrInvalidCredentials):
		writeError(w, http.StatusUnauthorized, "invalid_credentials",
			"Invalid username or password.")
	case errors.Is(err, auth.ErrTooManyAttempts):
		writeError(w, http.StatusTooManyRequests, "too_many_attempts",
			"Too many failed attempts. Please try again later.")
	case errors.Is(err, auth.ErrAccountDisabled):
		writeError(w, http.StatusForbidden, "account_disabled", "This account is disabled.")
	case errors.Is(err, auth.ErrForbidden):
		writeError(w, http.StatusForbidden, "forbidden",
			"You are not allowed to perform this operation in this organization.")
	case errors.Is(err, auth.ErrUsernameTaken):
		writeError(w, http.StatusConflict, "username_taken", "This username is already taken.")
	case errors.Is(err, auth.ErrMembershipExists):
		writeError(w, http.StatusConflict, "member_exists",
			"This account is already a member of the tenant.")
	case errors.Is(err, auth.ErrNoMembership), errors.Is(err, auth.ErrMembershipNotFound):
		writeError(w, http.StatusForbidden, "no_membership",
			"No tenant identity is available for this account.")
	case errors.Is(err, auth.ErrInvalidRole):
		writeError(w, http.StatusBadRequest, "invalid_role", "Unsupported role.")
	case errors.Is(err, auth.ErrWeakPassword), errors.Is(err, auth.ErrValidation):
		writeError(w, http.StatusBadRequest, "validation_failed", err.Error())
	case errors.Is(err, auth.ErrTokenExpired):
		writeError(w, http.StatusUnauthorized, "token_expired", "Credentials expired.")
	case errors.Is(err, auth.ErrInvalidToken), errors.Is(err, auth.ErrWrongTokenType):
		writeError(w, http.StatusUnauthorized, "invalid_token", "Invalid credentials.")
	case errors.Is(err, auth.ErrUserNotFound):
		writeError(w, http.StatusNotFound, "user_not_found", "Account not found.")
	default:
		h.logger.Error("auth_internal_error", zap.Error(err))
		writeError(w, http.StatusInternalServerError, "internal_error",
			"Unexpected error. Please retry.")
	}
}

// decodeJSON parses a JSON body under the platform's size limit, replying with
// 400 on malformed or oversized input.
func decodeJSON(w http.ResponseWriter, r *http.Request, target interface{}) bool {
	return httperr.DecodeJSON(w, r, target)
}

func writeJSON(w http.ResponseWriter, status int, payload interface{}) {
	httperr.WriteJSON(w, status, payload)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	httperr.WriteError(w, status, code, message)
}

// clientIP resolves the caller address for rate-limit bucketing. RealIP
// middleware already normalizes proxy headers into RemoteAddr.
func clientIP(r *http.Request) string {
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}
