// Package handler — declarative route configuration for validated proxy routes.
// Instead of writing one handler function per validated GET/DELETE endpoint,
// routes are declared as data in ValidatedRoute structs and registered
// programmatically via MakeValidatedProxy.
package handler

import (
	"fmt"
	"net/http"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/middleware"
	"github.com/datacode/gateway/internal/rbac"
)

// ValidatedRoute describes a route that requires certain query parameters
// to be present before the request is forwarded to the backend.
//
// Fields:
//   - Method:   HTTP method ("GET", "DELETE", …)
//   - Pattern:  chi route pattern relative to the route group
//     (e.g. "/models", "/apply/history")
//   - Required: query parameter names that must be non-empty
type ValidatedRoute struct {
	Method   string
	Pattern  string
	Required []string
}

// MakeValidatedProxy creates a handler that validates the presence of
// required query parameters and then transparently forwards the request
// (including all query params and body) to the backend described by target.
//
// All query parameters — not just the required ones — are forwarded as-is.
// This makes the gateway forward-compatible: new optional params added on the
// backend are automatically passed through without a gateway code change.
//
// Upstream resolution and failure handling mirror GenericProxy exactly: the
// target's per-request resolver (lease routing) takes precedence over
// BaseURL, an unresolvable upstream answers 503 rather than falling back to
// the load-balanced Service, and a transport failure triggers the target's
// stale-route invalidation hook.
func MakeValidatedProxy(target ProxyTarget, route ValidatedRoute, logger *zap.Logger) http.HandlerFunc {
	rewrite := target.PathRewrite
	if rewrite == nil {
		rewrite = IdentityPath()
	}

	return func(w http.ResponseWriter, r *http.Request) {
		// Validate required query params
		for _, param := range route.Required {
			if r.URL.Query().Get(param) == "" {
				logger.Debug("validated_proxy_missing_param",
					zap.String("path", r.URL.Path),
					zap.String("param", param))
				http.Error(w,
					fmt.Sprintf(`{"error":"%s is required"}`, param),
					http.StatusBadRequest)
				return
			}
		}

		// Defence in depth: a working copy belongs to exactly one user, so the
		// caller may only address their own directory.
		if !assertWorkspacePathOwnership(w, r, logger) {
			return
		}

		// Lease-aware upstream resolution: a validated route addresses a
		// working copy just like a catch-all one, so it must be routed to the
		// copy's owner replica, not load balanced. Contract verdicts keep
		// their own status (see writeResolutionError).
		baseURL, err := target.upstreamFor(r)
		if err != nil {
			writeResolutionError(w, err, logger)
			return
		}

		// Build backend path with query string
		backendPath := rewrite(r.URL.Path)
		if r.URL.RawQuery != "" {
			backendPath += "?" + r.URL.RawQuery
		}

		dispatchWithStaleRouteRetry(w, r, target, baseURL, backendPath, logger)
	}
}

// assertWorkspacePathOwnership verifies that a `workspace_path` query parameter
// addresses the caller's own working copy.
//
// Working copies live at `{base}/{tenant}/{workspace}/{user}/envs/{env}`, so the
// owning user is encoded in the path itself. Containment and layout are
// re-validated by the backend; this check only stops one member from operating
// on another member's checkout, which holds their uncommitted work. Requests
// without the parameter are untouched.
func assertWorkspacePathOwnership(
	w http.ResponseWriter, r *http.Request, logger *zap.Logger,
) bool {
	workspacePath := r.URL.Query().Get("workspace_path")
	if workspacePath == "" {
		return true
	}
	userID := middleware.GetUserID(r.Context())
	if userID == "" {
		return true
	}
	if rbac.OwnsWorkingCopy(workspacePath, userID) {
		return true
	}

	logger.Warn("workspace_path_ownership_denied",
		zap.String("path", r.URL.Path),
		zap.String("user_id", userID))
	middleware.WriteForbidden(w, middleware.DenyWorkspacePathForeign, "")
	return false
}

// RegisterValidated registers every route in a table against the given router
// and target.
//
// The router used to spell this loop out ten times, once per route group; a
// copy that drifts (a forgotten Method call, a swapped target) registers a
// route that silently behaves differently from its nine siblings, so the loop
// lives here exactly once.
func RegisterValidated(r chi.Router, routes []ValidatedRoute, target ProxyTarget, logger *zap.Logger) {
	for _, route := range routes {
		r.Method(route.Method, route.Pattern, MakeValidatedProxy(target, route, logger))
	}
}

// ---------------------------------------------------------------------------
// Route tables — declarative configuration consumed by router.go
// ---------------------------------------------------------------------------

// SQLMeshValidatedRoutes defines validated routes under the /sqlmesh route
// group.  The /sqlmesh prefix is stripped before forwarding to the backend.
var SQLMeshValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/models", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/lineage", Required: []string{"workspace_path"}},
	// The engine-side environment view is a reconciliation/troubleshooting read.
	// The user-facing environment list is served by the registry API under
	// /api/v1/workspaces/{id}/environments.
	{Method: "GET", Pattern: "/engine/environments", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/test", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/table-diff", Required: []string{"workspace_path", "source", "target"}},
	{Method: "GET", Pattern: "/apply/history", Required: []string{"workspace_path"}},
}

// SQLMeshIdentityValidatedRoutes defines validated routes that live directly
// under /api/v1 (not under /sqlmesh) but are proxied to the SQLMesh Service
// with identity path (no prefix stripping).
var SQLMeshIdentityValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/gateways", Required: []string{"workspace_path"}},
	// Lightweight gateway connectivity probe. Only workspace_path is required;
	// the optional `gateway` query param selects a specific gateway (defaults
	// to the config.yaml default when omitted).
	{Method: "GET", Pattern: "/gateways/ping", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/schemas", Required: []string{"workspace_path"}},
}

// GitValidatedRoutes defines validated GET routes under the /git route group.
// These are proxied to the SQLMesh Service with identity path.
var GitValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/status", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/init-status", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/branches", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/log", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/commit-files", Required: []string{"workspace_path", "commit_hash"}},
	{Method: "GET", Pattern: "/commit-file-diff", Required: []string{"workspace_path", "commit_hash", "file_path"}},
}

// FilesValidatedRoutes defines validated routes under the /files route group.
var FilesValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/read", Required: []string{"workspace_path", "file_path"}},
}

// DepsValidatedRoutes defines the working-copy dependency reads under /deps.
//
// Only /status is here. /deps/baseline deliberately takes NO workspace_path:
// the platform baseline is a property of the runtime image, identical for every
// working copy, so requiring one would force a client to name a copy in order
// to ask a question that has nothing to do with it — and would send the request
// through lease routing for no reason.
var DepsValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/status", Required: []string{"workspace_path"}},
}

// WipValidatedRoutes defines the WIP durability-contract reads (design 07 Q6).
// Every endpoint addresses one working copy through workspace_path, so they are
// lease-routed like the rest of the working-copy surface.
var WipValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/status", Required: []string{"workspace_path"}},
}

// LogsValidatedRoutes defines the workspace log reads under /logs. Every one
// requires workspace_path: a log query without a resolvable workspace has no
// legitimate meaning, and letting one through would hand the caller whichever
// workspace the backend happened to pick.
var LogsValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/errors", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/errors/{errorID}", Required: []string{"workspace_path"}},
}

// KnowledgeValidatedRoutes defines validated GET routes under the /knowledge
// route group. These are proxied to the SQLMesh Service with identity path.
var KnowledgeValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/documents/read", Required: []string{"workspace_path", "document_id"}},
}

// KnowledgeMetadataValidatedRoutes are the knowledge routes that read pure
// metadata (design 06 P8): the control plane answers them, load-balanced,
// because no working copy is involved.
var KnowledgeMetadataValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/documents/list", Required: []string{"workspace_path"}},
}

// OntologyValidatedRoutes defines validated GET routes under the /ontology
// route group. These are proxied to the Ontology Service with identity path.
// All ontology endpoints require workspace_path for workspace isolation.
var OntologyValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/*", Required: []string{"workspace_path"}},
}

// DagsterValidatedRoutes defines validated GET routes under the /dagster route
// group. Patterns are relative to the "/dagster" chi sub-router. These require
// workspace_path for per-workspace scoping, mirroring the SQLMesh lineage and
// Ontology endpoints. Everything else (deployment ensure/reload/delete, run
// launch/status/logs/cancel, health, mode) rides the catch-all GenericProxy.
var DagsterValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/assets/graph", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/schedules", Required: []string{"workspace_path"}},
	// A reload is an asynchronous job whose record lives in the working copy, so
	// only the replica holding that copy's lease can answer for it. Listed here
	// rather than left to the catch-all so a missing workspace_path is a 400 with
	// a reason, instead of a request routed to whichever replica DNS returned —
	// which would answer "no such job" for a job that is running fine.
	{Method: "GET", Pattern: "/deployments/reload/status", Required: []string{"workspace_path"}},
}
