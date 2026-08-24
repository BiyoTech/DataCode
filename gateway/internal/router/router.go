// Package router sets up the Chi HTTP router with all routes and middleware.
//
// Route registration follows a three-layer strategy:
//  1. Validated routes  — declared as data in handler.*ValidatedRoutes tables,
//     registered via handler.MakeValidatedProxy.
//  2. Special handlers  — routes that need body injection or SSE streaming.
//  3. Catch-all proxy   — handler.GenericProxy transparently forwards
//     everything else, so new backend endpoints are
//     automatically available without gateway code changes.
package router

import (
	"github.com/go-chi/chi/v5"
	chimiddleware "github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/handler"
	"github.com/datacode/gateway/internal/middleware"
	"github.com/datacode/gateway/internal/rbac"
)

// Setup creates and configures the Chi router with all API routes.
// authService owns identity verification; every non-public route requires a
// valid access token. rbacService owns authorization: it is enforced once,
// globally, by the Authorize middleware.
func Setup(
	cfg *config.Config, logger *zap.Logger,
	authService *auth.Service, rbacService *rbac.Service,
) *chi.Mux {
	r := chi.NewRouter()

	// Global middleware
	r.Use(chimiddleware.RequestID)
	r.Use(chimiddleware.RealIP)
	r.Use(middleware.RequestLogger(logger))
	r.Use(chimiddleware.Recoverer)

	// CORS configuration for frontend
	r.Use(cors.Handler(cors.Options{
		AllowedOrigins:   []string{cfg.CORS.FrontendOrigin, "http://localhost:*"},
		AllowedMethods:   []string{"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"},
		AllowedHeaders:   []string{"Accept", "Authorization", "Content-Type", "X-Trace-ID"},
		ExposedHeaders:   []string{"Link"},
		AllowCredentials: true,
		MaxAge:           300,
	}))

	// Authentication — must run after CORS so preflight requests still work.
	// It publishes the verified identity and overwrites downstream identity
	// headers, making client-supplied values unusable.
	r.Use(middleware.JWTAuth(authService.Issuer(), logger))

	// Replace any user_id carried in bodies/query strings with the verified
	// identity, so proxied services cannot be tricked by a forged owner.
	r.Use(middleware.SanitizeIdentity(logger))

	// Authorization — a single enforcement point for the whole platform. It
	// runs before route matching, so the transparent catch-all proxies are
	// covered as well; the required permission per route is declared in
	// rbac.MatchRoute.
	r.Use(middleware.Authorize(rbacService, logger))

	authHandler := handler.NewAuthHandler(authService, rbacService, logger)
	rbacHandler := handler.NewRBACHandler(rbacService, logger)

	// Health check (optional deep upstream probe via ?deep=1)
	r.Get("/health", handler.HealthCheck(cfg, logger))

	// Define proxy targets — each target knows the backend URL and how to
	// rewrite the path.
	sqlmeshTarget := handler.ProxyTarget{
		BaseURL:     cfg.Services.SQLMeshServiceURL,
		PathRewrite: handler.StripPrefix("/sqlmesh"),
	}
	sqlmeshIdentity := handler.ProxyTarget{
		BaseURL:     cfg.Services.SQLMeshServiceURL,
		PathRewrite: handler.IdentityPath(),
	}
	agentTarget := handler.ProxyTarget{
		BaseURL:     cfg.Services.AgentServiceURL,
		PathRewrite: handler.IdentityPath(),
	}
	ontologyTarget := handler.ProxyTarget{
		BaseURL:     cfg.Services.OntologyServiceURL,
		PathRewrite: handler.IdentityPath(),
	}
	dagsterTarget := handler.ProxyTarget{
		BaseURL:     cfg.Services.DagsterServiceURL,
		PathRewrite: handler.IdentityPath(),
	}

	// API routes
	r.Route("/api/v1", func(r chi.Router) {
		// -----------------------------------------------------------------
		// Authentication (gateway-owned, no proxy)
		// -----------------------------------------------------------------
		r.Route("/auth", func(r chi.Router) {
			// Public — reachable without an access token (see JWTAuth
			// whitelist); select-tenant additionally accepts a pre-auth token.
			r.Post("/register", authHandler.Register)
			r.Post("/login", authHandler.Login)
			r.Post("/select-tenant", authHandler.SelectTenant)
			r.Post("/refresh", authHandler.Refresh)

			// Authenticated
			r.Post("/logout", authHandler.Logout)
			r.Post("/change-password", authHandler.ChangePassword)
			r.Get("/memberships", authHandler.ListMemberships)

			// Minimal tenant directory for workspace-owner member pickers.
			// Any authenticated tenant member may read it.
			r.Get("/tenant-members", authHandler.ListTenantMemberCandidates)

			// Tenant member management — guarded by the tenant:member:*
			// permissions declared in the route map.
			r.Get("/members", authHandler.ListMembers)
			r.Post("/members", authHandler.AddMember)
		})

		// Local handler (no proxy)
		r.Get("/me", authHandler.CurrentUser)

		// -----------------------------------------------------------------
		// Role management (gateway-owned, no proxy)
		// -----------------------------------------------------------------
		r.Route("/rbac", func(r chi.Router) {
			// The permission catalog is read-only: it is declared in code and
			// synchronized into the database on startup.
			r.Get("/permissions", rbacHandler.ListPermissions)
			r.Get("/roles", rbacHandler.ListRoles)
			r.Post("/roles", rbacHandler.CreateRole)
			r.Put("/roles/{roleID}", rbacHandler.UpdateRole)
			r.Delete("/roles/{roleID}", rbacHandler.DeleteRole)
		})

		// -----------------------------------------------------------------
		// Agent Service — sessions, chat, SSE
		// -----------------------------------------------------------------
		r.Route("/sessions", func(r chi.Router) {
			// SSE — must be registered before catch-all (chi gives priority
			// to more specific patterns regardless of registration order)
			r.Get("/{sessionID}/events", handler.AgentSSEEvents(cfg, logger))

			// Catch-all transparent proxy for all other session routes:
			//   POST /               → backend /api/v1/sessions
			//   GET  /               → backend /api/v1/sessions
			//   GET  /{sessionID}    → backend /api/v1/sessions/{sessionID}
			//   GET  /{sessionID}/messages
			//   POST /{sessionID}/mode | /chat | /approve | /cancel
			r.HandleFunc("/*", handler.GenericProxy(agentTarget, logger))
		})

		// LLM models (Agent Service, identity path)
		r.Get("/models", handler.GenericProxy(agentTarget, logger))

		// -----------------------------------------------------------------
		// SQLMesh Service — /sqlmesh/* (strip /sqlmesh prefix)
		// -----------------------------------------------------------------
		r.Route("/sqlmesh", func(r chi.Router) {
			// Validated routes — check required query params, then proxy
			for _, route := range handler.SQLMeshValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(sqlmeshTarget, route, logger))
			}

			// SSE — streaming proxy with dedicated HTTP client
			r.Get("/events", handler.SSEEvents(cfg, logger))

			// Catch-all transparent proxy for pure-passthrough routes:
			//   POST /plan | /apply | /apply/async | /cancel
			//   POST /render | /validate | /evaluate | /query
			//   POST /schema/discover
			//   GET  /meta | /modules
			//   GET  /apply/history/{recordId}
			r.HandleFunc("/*", handler.GenericProxy(sqlmeshTarget, logger))
		})

		// -----------------------------------------------------------------
		// SQLMesh Service — identity routes (not under /sqlmesh)
		// -----------------------------------------------------------------
		for _, route := range handler.SQLMeshIdentityValidatedRoutes {
			r.Method(route.Method, route.Pattern,
				handler.MakeValidatedProxy(sqlmeshIdentity, route, logger))
		}
		// Pure passthrough
		r.Post("/external-models/import", handler.GenericProxy(sqlmeshIdentity, logger))

		// -----------------------------------------------------------------
		// Workspace management (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/workspaces", func(r chi.Router) {
			// Special handlers — identity injection (user_id + tenant_id)
			r.Post("/", handler.CreateWorkspace(cfg, rbacService, logger))
			r.Get("/", handler.ListWorkspaces(cfg, rbacService, logger))
			r.Post("/bind-git", handler.BindGitRepo(cfg, logger))
			r.Post("/sync", handler.SyncWorkspace(cfg, logger))

			// Open a workspace: clone/fetch the caller's own working copy.
			r.Post("/{workspaceID}/open", handler.OpenWorkspace(cfg, logger))
			r.Get("/{workspaceID}/remote-branches", handler.ListWorkspaceRemoteBranches(cfg, logger))

			// Environment registry — the workspace's development lines. The
			// backend derives tenant/user from the identity headers, so these
			// are pure passthrough.
			r.HandleFunc("/{workspaceID}/environments", handler.GenericProxy(sqlmeshIdentity, logger))

			// Membership management — owned by the gateway, which holds the
			// role bindings; authorization is enforced by the route map.
			r.Get("/{workspaceID}/members", handler.ListWorkspaceMembers(authService, rbacService, logger))
			r.Post("/{workspaceID}/members", handler.AddWorkspaceMember(authService, rbacService, logger))
			r.Delete("/{workspaceID}/members/{memberUserID}",
				handler.RemoveWorkspaceMember(cfg, rbacService, logger))

			r.Delete("/{workspaceID}", handler.DeleteWorkspace(cfg, rbacService, logger))
		})

		// -----------------------------------------------------------------
		// Environment registry (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		// Addressed by environment id ("{workspace_id}:{environment}"), from
		// which the route map derives the workspace to authorize against.
		//   GET    /{envID}          | DELETE /{envID}
		//   POST   /{envID}/open     | GET    /{envID}/events
		r.HandleFunc("/environments/*", handler.GenericProxy(sqlmeshIdentity, logger))

		// -----------------------------------------------------------------
		// Git operations (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/git", func(r chi.Router) {
			// Validated GET routes
			for _, route := range handler.GitValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(sqlmeshIdentity, route, logger))
			}

			// Special handlers — user_id from context
			r.Get("/credentials", handler.ListGitCredentials(cfg, logger))
			r.Post("/credentials", handler.CreateGitCredential(cfg, logger))
			r.Delete("/credentials/{credentialID}", handler.DeleteGitCredential(cfg, logger))
			r.Patch("/credentials/{credentialID}/default", handler.SetDefaultGitCredential(cfg, logger))

			// Pure passthrough PUT (no user_id injection needed)
			r.Put("/credentials/{credentialID}", handler.GenericProxy(sqlmeshIdentity, logger))

			// Catch-all transparent proxy for pure-passthrough git routes:
			//   POST /init | /stage | /unstage | /commit | /push | /pull
			//   POST /checkout | /publish
			r.HandleFunc("/*", handler.GenericProxy(sqlmeshIdentity, logger))
		})

		// -----------------------------------------------------------------
		// File operations (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/files", func(r chi.Router) {
			// Validated routes
			for _, route := range handler.FilesValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(sqlmeshIdentity, route, logger))
			}

			// Catch-all transparent proxy for:
			//   GET  /list (forwards all query params)
			//   POST /write | /delete | /mkdir | /rename
			r.HandleFunc("/*", handler.GenericProxy(sqlmeshIdentity, logger))
		})

		// -----------------------------------------------------------------
		// Workspace logs (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/logs", func(r chi.Router) {
			// SSE tail — dedicated handler, the streaming client has no timeout.
			r.Get("/stream", handler.SSELogs(cfg, logger))

			for _, route := range handler.LogsValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(sqlmeshIdentity, route, logger))
			}
		})

		// -----------------------------------------------------------------
		// Knowledge documents (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/knowledge", func(r chi.Router) {
			// Validated GET routes
			for _, route := range handler.KnowledgeValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(sqlmeshIdentity, route, logger))
			}

			// Catch-all transparent proxy for:
			//   POST /documents/create | /documents/upload | /documents/delete
			r.HandleFunc("/*", handler.GenericProxy(sqlmeshIdentity, logger))
		})

		// -----------------------------------------------------------------
		// Ontology Service — /ontology/* (identity path)
		// -----------------------------------------------------------------
		r.Route("/ontology", func(r chi.Router) {
			// Validated routes — all ontology endpoints require workspace_path
			for _, route := range handler.OntologyValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(ontologyTarget, route, logger))
			}
		})

		// -----------------------------------------------------------------
		// Dagster Service — /dagster/* (identity path)
		// -----------------------------------------------------------------
		r.Route("/dagster", func(r chi.Router) {
			// Validated GET routes — require workspace_path for scoping
			for _, route := range handler.DagsterValidatedRoutes {
				r.Method(route.Method, route.Pattern,
					handler.MakeValidatedProxy(dagsterTarget, route, logger))
			}

			// Catch-all transparent proxy for pure-passthrough routes:
			//   GET    /health | /mode | /assets | /deployments | /deployments/diff
			//   GET    /runs/{runID} | /runs/{runID}/logs
			//   POST   /pipelines/run | /deployments/ensure | /deployments/reload
			//   POST   /runs/{runID}/cancel
			//   DELETE /deployments/{deployment}
			r.HandleFunc("/*", handler.GenericProxy(dagsterTarget, logger))
		})
	})

	return r
}
