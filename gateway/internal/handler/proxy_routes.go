// Package handler — declarative route configuration for validated proxy routes.
// Instead of writing one handler function per validated GET/DELETE endpoint,
// routes are declared as data in ValidatedRoute structs and registered
// programmatically via MakeValidatedProxy.
package handler

import (
	"fmt"
	"io"
	"net/http"

	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/middleware"
	"github.com/agenticdata/gateway/internal/rbac"
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

		// Build backend path with query string
		backendPath := rewrite(r.URL.Path)
		if r.URL.RawQuery != "" {
			backendPath += "?" + r.URL.RawQuery
		}

		switch r.Method {
		case http.MethodGet:
			proxyGet(w, r, target.BaseURL, backendPath, logger)
		case http.MethodDelete:
			proxyDelete(w, r, target.BaseURL, backendPath, logger)
		case http.MethodPost:
			proxyPost(w, r, target.BaseURL, backendPath, readBodyOrError(w, r, logger), logger)
		case http.MethodPut:
			proxyPut(w, r, target.BaseURL, backendPath, readBodyOrError(w, r, logger), logger)
		case http.MethodPatch:
			proxyPatch(w, r, target.BaseURL, backendPath, readBodyOrError(w, r, logger), logger)
		default:
			http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		}
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

// readBodyOrError reads the request body or writes a 400 error and returns nil.
// Shared helper for MakeValidatedProxy body-bearing methods.
func readBodyOrError(w http.ResponseWriter, r *http.Request, logger *zap.Logger) []byte {
	body, err := readRequestBody(r)
	if err != nil {
		logger.Warn("validated_proxy_read_body_failed",
			zap.String("method", r.Method),
			zap.String("path", r.URL.Path),
			zap.Error(err))
		http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
		return nil
	}
	return body
}

// readRequestBody safely reads and closes the request body.
func readRequestBody(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	return io.ReadAll(r.Body)
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
	{Method: "GET", Pattern: "/documents/list", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/documents/read", Required: []string{"workspace_path", "document_id"}},
}

// OntologyValidatedRoutes defines validated GET routes under the /ontology
// route group. These are proxied to the Ontology Service with identity path.
// All ontology endpoints require workspace_path for workspace isolation.
var OntologyValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/*", Required: []string{"workspace_path"}},
}
