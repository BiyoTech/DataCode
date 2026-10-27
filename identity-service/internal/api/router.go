// Package api — HTTP router for identity-service.
//
// Two distinct surfaces:
//
//   - PUBLIC / user-facing (`/auth/*`, `/me`, `/rbac/*`, tenant + workspace
//     membership): reached through the gateway, which proxies them here.
//   - INTERNAL (`/internal/*`): service-to-service, shared-token guarded, not
//     under /api/v1 so no gateway forwards it.
//
// Authorization note: identity-service does NOT run the platform's Authorize
// middleware. It is the service that ANSWERS authorization questions, so
// depending on itself for them would be circular. Its own endpoints are
// protected by (a) JWT verification, and (b) each handler asserting the
// caller's tenant scope from verified claims.
package api

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	chimiddleware "github.com/go-chi/chi/v5/middleware"
	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/auth"
)

// Router assembles the identity-service HTTP surface.
func Router(
	authService *auth.Service,
	authHandler *AuthHandler,
	rbacHandler *RBACHandler,
	internalHandler *InternalHandler,
	logger *zap.Logger,
) *chi.Mux {
	r := chi.NewRouter()

	r.Use(chimiddleware.RequestID)
	r.Use(chimiddleware.RealIP)
	r.Use(chimiddleware.Recoverer)

	r.Get("/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"status":       "healthy",
			"service":      "identity-service",
			"internal_api": internalHandler.Enabled(),
		})
	})

	// Internal, token-guarded (fail closed when unconfigured).
	r.Mount("/internal", internalHandler.Routes())

	// Public + authenticated surface. JWTAuth publishes the verified identity
	// into the context; handlers read it via GetUserID/GetTenantID.
	r.Group(func(r chi.Router) {
		r.Use(JWTAuth(authService.Issuer(), logger))

		r.Route("/auth", func(r chi.Router) {
			// Public (the JWTAuth whitelist covers these paths).
			r.Post("/register", authHandler.Register)
			r.Post("/login", authHandler.Login)
			r.Post("/select-tenant", authHandler.SelectTenant)
			r.Post("/refresh", authHandler.Refresh)

			// Authenticated.
			r.Post("/logout", authHandler.Logout)
			r.Post("/change-password", authHandler.ChangePassword)
			r.Get("/memberships", authHandler.ListMemberships)
			r.Get("/tenant-members", authHandler.ListTenantMemberCandidates)
			r.Get("/members", authHandler.ListMembers)
			r.Post("/members", authHandler.AddMember)
		})

		r.Get("/me", authHandler.CurrentUser)
		r.Patch("/me/preferences", authHandler.UpdatePreferences)

		r.Route("/rbac", func(r chi.Router) {
			r.Get("/permissions", rbacHandler.ListPermissions)
			r.Get("/roles", rbacHandler.ListRoles)
			r.Post("/roles", rbacHandler.CreateRole)
			r.Put("/roles/{roleID}", rbacHandler.UpdateRole)
			r.Delete("/roles/{roleID}", rbacHandler.DeleteRole)
		})
	})

	return r
}
