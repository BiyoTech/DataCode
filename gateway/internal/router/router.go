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
	"net/http"

	"github.com/go-chi/chi/v5"
	chimiddleware "github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
	"github.com/datacode/gateway/internal/authz"
	"github.com/datacode/gateway/internal/cellrouter"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/handler"
	"github.com/datacode/gateway/internal/leaserouter"
	"github.com/datacode/gateway/internal/middleware"
)

// Setup creates and configures the Chi router with all API routes.
//
// issuer verifies the access tokens identity-service minted; every non-public
// route requires a valid one. Authorization is enforced once, globally, by the
// Authorize middleware, which decides against an authorization SNAPSHOT
// fetched from identity-service (P2, ADR-007) rather than reading a database.
//
// bindings performs the authorization-binding WRITES the gateway used to do
// against its own tables (P5.5): identity-service is the single writer of the
// identity dataset, which is what makes the P7 Global/Cell split possible.
//
// leaseRouter resolves working-copy requests to the single runtime replica that
// owns them (design 06 P4a). A nil/disabled router keeps every proxy target on
// its static BaseURL, which is the pre-P4a behaviour.
//
// cellRouter refuses requests whose tenant lives in ANOTHER cell (design 06 P7),
// answering 421 with that cell's address. A nil router serves everything
// locally, which is correct for a single-cell installation.
func Setup(
	cfg *config.Config, logger *zap.Logger,
	issuer *auth.TokenIssuer, authzResolver *authz.Resolver, bindings *authz.Bindings,
	leaseRouter *leaserouter.Router, cellRouter *cellrouter.Router,
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
	r.Use(middleware.JWTAuth(issuer, logger))

	// Cell routing — after authentication (it reads the VERIFIED cell claim)
	// and before authorization (a request this cell must not serve should not
	// consume an authorization lookup, and the caller's permissions here are
	// meaningless: their tenant's data lives in another cell).
	if cellRouter != nil {
		r.Use(cellRouter.Middleware(func(req *http.Request) string {
			return middleware.GetCellID(req.Context())
		}))
	}

	// Replace any user_id carried in bodies/query strings with the verified
	// identity, so proxied services cannot be tricked by a forged owner.
	r.Use(middleware.SanitizeIdentity(logger))

	// Authorization — a single enforcement point for the whole platform. It
	// runs before route matching, so the transparent catch-all proxies are
	// covered as well; the required permission per route is declared in
	// rbac.MatchRoute.
	r.Use(middleware.Authorize(authzResolver, logger))

	// The identity surface is served by identity-service; the gateway only
	// forwards it (P5.5). Registered once and reused by the /auth, /me and
	// /rbac groups below.
	identityProxy := handler.IdentityProxy(cfg.Services.IdentityServiceURL, logger)

	// Health check (optional deep upstream probe via ?deep=1)
	r.Get("/health", handler.HealthCheck(cfg, logger))

	// Define proxy targets — each target knows the backend URL and how to
	// rewrite the path.
	//
	// P3: workspace-runtime is the merged owner of working copies (sqlmesh
	// engine + ontology + dagster data plane + git + files + tools), and
	// every working-copy-facing group targets it directly. The legacy
	// per-service entries were removed from platform.yaml along with the
	// services they named, so there is no fallback to configure — a missing
	// services.runtime registration surfaces as proxy errors, not as traffic
	// silently routed to the wrong place.
	runtimeURL := cfg.Services.RuntimeServiceURL

	// P5: control-service owns the control plane (environment registry,
	// workspace metadata, Git credentials). It holds no working copy, so its
	// routes are plain load-balanced — no lease resolution. A missing
	// services.control registration surfaces as proxy errors rather than
	// traffic silently routed elsewhere.
	controlURL := cfg.Services.ControlServiceURL

	// P4a: a working copy is owned by exactly ONE workspace-runtime replica, so
	// requests for it must reach that replica rather than being load balanced.
	// When the lease subsystem is off (no Redis, or pre-P4a deployment) the
	// resolver returns "" and every target keeps its static BaseURL, which
	// reproduces the previous behaviour exactly.
	resolveWorkingCopyUpstream := func(r *http.Request) (string, error) {
		if !leaseRouter.Enabled() {
			return "", nil
		}
		// Read from context: Authorize already extracted it (possibly from the
		// JSON body) and buffering the body a second time here is how a proxy
		// starts forwarding empty POSTs.
		workspacePath := middleware.GetWorkspacePath(r.Context())
		if workspacePath == "" {
			return "", nil
		}
		return leaseRouter.Resolve(r.Context(), workspacePath)
	}
	invalidateWorkingCopyRoute := func(r *http.Request) {
		if leaseRouter.Enabled() {
			leaseRouter.Invalidate(middleware.GetWorkspacePath(r.Context()))
		}
	}

	// One shape for every working-copy-affine route group: the lease decides
	// which replica serves the request, and a forward failure invalidates the
	// cached route so the next attempt re-resolves. Prefix handling is the only
	// axis that varies across the groups — keeping a single constructor is what
	// guarantees a change to lease semantics reaches every group at once
	// (four hand-written copies here were how one of them drifted before).
	workingCopyTarget := func(rewrite handler.PathRewriteFunc) handler.ProxyTarget {
		return handler.ProxyTarget{
			BaseURL:          runtimeURL,
			PathRewrite:      rewrite,
			ResolveUpstream:  resolveWorkingCopyUpstream,
			OnForwardFailure: invalidateWorkingCopyRoute,
		}
	}
	sqlmeshTarget := workingCopyTarget(handler.StripPrefix("/sqlmesh"))
	// Every identity-path working-copy group (the sqlmesh/git/files/logs/
	// knowledge identity routes, ontology, dagster, WIP) shares this ONE
	// target: they differ in which validated route table selects them, not in
	// routing behaviour.
	runtimeIdentity := workingCopyTarget(handler.IdentityPath())
	agentTarget := handler.ProxyTarget{
		// agent-service is stateless: it owns no working copy, so it stays
		// load balanced.
		BaseURL:     cfg.Services.AgentServiceURL,
		PathRewrite: handler.IdentityPath(),
	}

	// P5: the control plane's identity-path routes. Plain load balancing: the
	// registry is database-backed state, every replica can answer.
	controlIdentity := handler.ProxyTarget{
		BaseURL:     controlURL,
		PathRewrite: handler.IdentityPath(),
	}

	// API routes
	r.Route("/api/v1", func(r chi.Router) {
		// -----------------------------------------------------------------
		// Identity surface — proxied to identity-service (P5.5)
		// -----------------------------------------------------------------
		// Authentication, the current-user payload and role management all
		// read and write the identity dataset, which identity-service owns
		// exclusively. The gateway still AUTHORIZES these routes (the route
		// map declares tenant:member:* / tenant:role:* for the member and
		// role endpoints) and still authenticates them; it just no longer
		// keeps its own copy of the tables behind them.
		//
		// Registered as nested Route + "/*" rather than HandleFunc("/auth/*"):
		// chi matches a wildcard pattern only within the routing subtree that
		// declares it, so the flat form silently 404s every /auth request.
		r.Route("/auth", func(r chi.Router) {
			r.HandleFunc("/*", identityProxy)
		})
		r.Route("/me", func(r chi.Router) {
			// The bare /me path plus its sub-resources (/me/preferences).
			r.HandleFunc("/", identityProxy)
			r.HandleFunc("/*", identityProxy)
		})
		r.Route("/rbac", func(r chi.Router) {
			r.HandleFunc("/*", identityProxy)
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
			handler.RegisterValidated(r, handler.SQLMeshValidatedRoutes, sqlmeshTarget, logger)

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
		handler.RegisterValidated(r, handler.SQLMeshIdentityValidatedRoutes, runtimeIdentity, logger)
		// Pure passthrough
		r.Post("/external-models/import", handler.GenericProxy(runtimeIdentity, logger))

		// -----------------------------------------------------------------
		// Workspace management (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/workspaces", func(r chi.Router) {
			// Special handlers — identity injection (user_id + tenant_id)
			r.Post("/", handler.CreateWorkspace(cfg, bindings, authzResolver, logger))
			r.Get("/", handler.ListWorkspaces(cfg, authzResolver, logger))
			r.Post("/bind-git", handler.BindGitRepo(cfg, logger))
			r.Post("/sync", handler.SyncWorkspace(cfg, logger))

			// Open a workspace: clone/fetch the caller's own working copy.
			r.Post("/{workspaceID}/open", handler.OpenWorkspace(cfg, logger))
			r.Get("/{workspaceID}/remote-branches", handler.ListWorkspaceRemoteBranches(cfg, logger))

			// Environment registry — the workspace's development lines. The
			// backend derives tenant/user from the identity headers, so these
			// are pure passthrough (control-service, P5).
			r.HandleFunc("/{workspaceID}/environments", handler.GenericProxy(controlIdentity, logger))

			// Membership management — the bindings live in identity-service
			// (P5.5); authorization is enforced by the route map.
			r.Get("/{workspaceID}/members", handler.ListWorkspaceMembers(bindings, logger))
			r.Post("/{workspaceID}/members",
				handler.AddWorkspaceMember(bindings, authzResolver, logger))
			r.Delete("/{workspaceID}/members/{memberUserID}",
				handler.RemoveWorkspaceMember(cfg, bindings, authzResolver, logger))

			r.Delete("/{workspaceID}", handler.DeleteWorkspace(cfg, bindings, authzResolver, logger))
		})

		// -----------------------------------------------------------------
		// Tenant quota and usage (control-service, identity path — P8)
		// -----------------------------------------------------------------
		// Scoped to the caller's own tenant by the identity header, so no
		// permission beyond authentication is required (contract 08 §6): the
		// identity IS the scope, and accepting a tenant id would turn this into
		// an enumeration surface.
		r.HandleFunc("/quotas", handler.GenericProxy(controlIdentity, logger))

		// -----------------------------------------------------------------
		// Environment registry (control-service, identity path — P5)
		// -----------------------------------------------------------------
		// Addressed by environment id ("{workspace_id}:{environment}"), from
		// which the route map derives the workspace to authorize against.
		//   GET    /{envID}          | DELETE /{envID}
		//   POST   /{envID}/open     | GET    /{envID}/events
		r.HandleFunc("/environments/*", handler.GenericProxy(controlIdentity, logger))

		// -----------------------------------------------------------------
		// Git operations (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/git", func(r chi.Router) {
			// Validated GET routes
			handler.RegisterValidated(r, handler.GitValidatedRoutes, runtimeIdentity, logger)

			// Credentials are user-scoped CONTROL PLANE data (P5), forwarded
			// transparently: the control plane resolves the owner from the
			// identity headers this gateway writes, and SanitizeIdentity has
			// already overwritten any owner a client tried to name. There is
			// nothing left for a per-method handler to add.
			r.HandleFunc("/credentials", handler.GenericProxy(controlIdentity, logger))
			r.HandleFunc("/credentials/*", handler.GenericProxy(controlIdentity, logger))

			// Catch-all transparent proxy for pure-passthrough git routes:
			//   POST /init | /stage | /unstage | /commit | /push | /pull
			//   POST /checkout | /publish
			r.HandleFunc("/*", handler.GenericProxy(runtimeIdentity, logger))
		})

		// -----------------------------------------------------------------
		// File operations (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/files", func(r chi.Router) {
			// Validated routes
			handler.RegisterValidated(r, handler.FilesValidatedRoutes, runtimeIdentity, logger)

			// Catch-all transparent proxy for:
			//   GET  /list (forwards all query params)
			//   POST /write | /delete | /mkdir | /rename
			r.HandleFunc("/*", handler.GenericProxy(runtimeIdentity, logger))
		})

		// -----------------------------------------------------------------
		// WIP durability contract (design 07 Q6) — the user-facing surface
		// of the lease subsystem's snapshot machinery.
		// -----------------------------------------------------------------
		r.Route("/wip", func(r chi.Router) {
			// Validated routes
			handler.RegisterValidated(r, handler.WipValidatedRoutes, runtimeIdentity, logger)

			// Catch-all transparent proxy for:
			//   POST /recover | /discard (the user's snapshot decision)
			r.HandleFunc("/*", handler.GenericProxy(runtimeIdentity, logger))
		})

		// -----------------------------------------------------------------
		// Workspace logs (SQLMesh Service, identity path)
		// -----------------------------------------------------------------
		r.Route("/logs", func(r chi.Router) {
			// SSE tail — dedicated handler, the streaming client has no timeout.
			r.Get("/stream", handler.SSELogs(cfg, logger))

			handler.RegisterValidated(r, handler.LogsValidatedRoutes, runtimeIdentity, logger)
		})

		// -----------------------------------------------------------------
		// Knowledge documents (identity path)
		//
		// Split along the P5 line (completed in P8): the LIST is pure
		// metadata over the shared database and answers from the control
		// plane (plain load balancing); everything else touches the working
		// copy's files and stays lease-routed to workspace-runtime.
		// -----------------------------------------------------------------
		r.Route("/knowledge", func(r chi.Router) {
			handler.RegisterValidated(r, handler.KnowledgeMetadataValidatedRoutes, controlIdentity, logger)

			// Validated GET routes — runtime (file content lives there)
			handler.RegisterValidated(r, handler.KnowledgeValidatedRoutes, runtimeIdentity, logger)

			// Catch-all transparent proxy for:
			//   POST /documents/create | /documents/upload | /documents/delete
			r.HandleFunc("/*", handler.GenericProxy(runtimeIdentity, logger))
		})

		// -----------------------------------------------------------------
		// Ontology Service — /ontology/* (identity path)
		// -----------------------------------------------------------------
		r.Route("/ontology", func(r chi.Router) {
			// Validated routes — all ontology endpoints require workspace_path
			handler.RegisterValidated(r, handler.OntologyValidatedRoutes, runtimeIdentity, logger)
		})

		// -----------------------------------------------------------------
		// Dagster Service — /dagster/* (identity path)
		// -----------------------------------------------------------------
		r.Route("/dagster", func(r chi.Router) {
			// Validated GET routes — require workspace_path for scoping
			handler.RegisterValidated(r, handler.DagsterValidatedRoutes, runtimeIdentity, logger)

			// Catch-all transparent proxy for pure-passthrough routes:
			//   GET    /health | /mode | /assets | /deployments | /deployments/diff
			//   GET    /runs/{runID} | /runs/{runID}/logs
			//   POST   /pipelines/run | /deployments/ensure | /deployments/reload
			//   POST   /runs/{runID}/cancel
			//   DELETE /deployments/{deployment}
			r.HandleFunc("/*", handler.GenericProxy(runtimeIdentity, logger))
		})
	})

	return r
}
