// Package handler — generic reverse proxy for transparent request forwarding.
// Instead of writing one handler function per endpoint, GenericProxy handles
// all pure-passthrough routes (GET/POST/PUT/PATCH/DELETE) with a single function.
// Path rewriting (e.g. stripping a route-group prefix) is configurable via ProxyTarget.
package handler

import (
	"io"
	"net/http"
	"strings"

	"go.uber.org/zap"
)

// PathRewriteFunc transforms the incoming gateway request path into the
// backend service path.  This is needed because some route groups use a
// prefix on the gateway side that the backend does not expect
// (e.g. /api/v1/sqlmesh/plan → /api/v1/plan).
type PathRewriteFunc func(gatewayPath string) (backendPath string)

// IdentityPath returns a rewrite function that leaves the path unchanged.
// Use this when the gateway path and the backend path are identical.
func IdentityPath() PathRewriteFunc {
	return func(p string) string { return p }
}

// StripPrefix returns a rewrite function that removes the first occurrence of
// the given prefix segment from the path.
//
// Example: StripPrefix("/sqlmesh") applied to "/api/v1/sqlmesh/plan"
// produces "/api/v1/plan".
func StripPrefix(prefix string) PathRewriteFunc {
	search := prefix + "/"
	return func(path string) string {
		idx := strings.Index(path, search)
		if idx == -1 {
			return path
		}
		return path[:idx] + path[idx+len(prefix):]
	}
}

// ProxyTarget describes where and how to proxy a request.
type ProxyTarget struct {
	// BaseURL is the upstream service base URL (e.g. "http://localhost:8002").
	BaseURL string
	// PathRewrite converts the gateway request path to the backend path.
	// Use IdentityPath() when no transformation is needed.
	PathRewrite PathRewriteFunc
}

// GenericProxy creates a transparent reverse-proxy handler.
//
// It forwards the full request — method, path (after rewriting), query string
// and body — to the target backend.  No validation or body mutation is
// performed; use MakeValidatedProxy for routes that require query-param
// validation, or write a dedicated handler for routes that need body
// injection (e.g. user_id) or SSE streaming.
func GenericProxy(target ProxyTarget, logger *zap.Logger) http.HandlerFunc {
	rewrite := target.PathRewrite
	if rewrite == nil {
		rewrite = IdentityPath()
	}

	return func(w http.ResponseWriter, r *http.Request) {
		backendPath := rewrite(r.URL.Path)
		if r.URL.RawQuery != "" {
			backendPath += "?" + r.URL.RawQuery
		}

		switch r.Method {
		case http.MethodGet:
			proxyGet(w, r, target.BaseURL, backendPath, logger)

		case http.MethodPost:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				logger.Warn("generic_proxy_read_body_failed",
					zap.String("method", r.Method),
					zap.String("path", r.URL.Path),
					zap.Error(err))
				http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
				return
			}
			r.Body.Close()
			proxyPost(w, r, target.BaseURL, backendPath, body, logger)

		case http.MethodPut:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				logger.Warn("generic_proxy_read_body_failed",
					zap.String("method", r.Method),
					zap.String("path", r.URL.Path),
					zap.Error(err))
				http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
				return
			}
			r.Body.Close()
			proxyPut(w, r, target.BaseURL, backendPath, body, logger)

		case http.MethodPatch:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				logger.Warn("generic_proxy_read_body_failed",
					zap.String("method", r.Method),
					zap.String("path", r.URL.Path),
					zap.Error(err))
				http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
				return
			}
			r.Body.Close()
			proxyPatch(w, r, target.BaseURL, backendPath, body, logger)

		case http.MethodDelete:
			proxyDelete(w, r, target.BaseURL, backendPath, logger)

		default:
			http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		}
	}
}
