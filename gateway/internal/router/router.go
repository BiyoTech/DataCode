// Package router sets up the Chi HTTP router with all routes and middleware.
//
// Route registration follows a three-layer strategy:
//  1. Validated routes  — declared as data in handler.*ValidatedRoutes tables,
//                         registered via handler.MakeValidatedProxy.
//  2. Special handlers  — routes that need body injection or SSE streaming.
//  3. Catch-all proxy   — handler.GenericProxy transparently forwards
//                         everything else, so new backend endpoints are
//                         automatically available without gateway code changes.
package router

import (
	"github.com/go-chi/chi/v5"
	chimiddleware "github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/config"
	"github.com/agenticdata/gateway/internal/handler"
	"github.com/agenticdata/gateway/internal/middleware"
)

// Setup creates and configures the Chi router with all API routes.
func Setup(cfg *config.Config, logger *zap.Logger) *chi.Mux {
	r := chi.NewRouter()

	// Global middleware
	r.Use(chimiddleware.RequestID)
	r.Use(chimiddleware.RealIP)
	r.Use(middleware.RequestLogger(logger))
	r.Use(chimiddleware.Recoverer)
	r.Use(middleware.DefaultUser)

	// CORS configuration for frontend
	r.Use(cors.Handler(cors.Options{
		AllowedOrigins:   []string{cfg.CORS.FrontendOrigin, "http://localhost:*"},
		AllowedMethods:   []string{"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"},
		AllowedHeaders:   []string{"Accept", "Authorization", "Content-Type", "X-Trace-ID"},
		ExposedHeaders:   []string{"Link"},
		AllowCredentials: true,
		MaxAge:           300,
	}))

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
		// Local handler (no proxy)
		r.Get("/me", handler.GetCurrentUser)

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
			// Special handlers — user_id injection into body
			r.Post("/", handler.CreateWorkspace(cfg, logger))
			r.Get("/", handler.ListWorkspaces(cfg, logger))
			r.Post("/bind-git", handler.BindGitRepo(cfg, logger))

			// Pure passthrough — sync, ensure-local, delete
			r.Post("/sync", handler.GenericProxy(sqlmeshIdentity, logger))
			r.Post("/{workspaceID}/ensure-local", handler.GenericProxy(sqlmeshIdentity, logger))
			r.Delete("/{workspaceID}", handler.GenericProxy(sqlmeshIdentity, logger))
		})

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
			//   GET  /health | /mode | /assets | /pipelines/code-locations
			//   GET  /runs/{runID} | /runs/{runID}/logs
			//   POST /pipelines/run | /pipelines/code-location/ensure
			//   POST /pipelines/code-location/reload | /runs/{runID}/cancel
			r.HandleFunc("/*", handler.GenericProxy(dagsterTarget, logger))
		})
	})

	return r
}
