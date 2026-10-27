// Package handler — SQLMesh SSE streaming handler.
//
// All pure-passthrough and simple-validated SQLMesh routes are handled
// declaratively via GenericProxy / MakeValidatedProxy in proxy_generic.go
// and proxy_routes.go. Only SSE streaming remains as a special handler
// because it requires the dedicated sseClient (no timeout) instead of the
// standard upstreamClient (25s timeout).
package handler

import (
	"net/http"
	"net/url"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/config"
)

// SSEEvents proxies the Server-Sent Events stream from the working-copy owner
// (workspace-runtime since P3). Streams events directly to the client without
// buffering for real-time updates.
func SSEEvents(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		targetPath := "/api/v1/events"
		if ws := r.URL.Query().Get("workspace_path"); ws != "" {
			targetPath += "?workspace_path=" + url.QueryEscape(ws)
		}
		proxySSE(w, r, workingCopyUpstream(cfg), targetPath, nil, logger)
	}
}

// logStreamQueryParams are the filters the log tail accepts. They are copied
// explicitly rather than forwarding the raw query string so an unexpected
// parameter can never reach the upstream unchecked.
var logStreamQueryParams = []string{"workspace_path", "sources", "levels", "q"}

// SSELogs proxies the workspace log tail from SQLMesh Service.
//
// Separate from SSEEvents because it is a different resource with a different
// permission (workspace:logs:read): events carry plan and file activity, this
// carries raw service log lines.
func SSELogs(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// The validated-proxy path applies this to the other log routes; the
		// tail needs it too, because a working copy's logs are as private as
		// the uncommitted work they describe.
		if !assertWorkspacePathOwnership(w, r, logger) {
			return
		}

		query := url.Values{}
		for _, name := range logStreamQueryParams {
			if value := r.URL.Query().Get(name); value != "" {
				query.Set(name, value)
			}
		}
		targetPath := "/api/v1/logs/stream"
		if encoded := query.Encode(); encoded != "" {
			targetPath += "?" + encoded
		}
		proxySSE(w, r, workingCopyUpstream(cfg), targetPath, nil, logger)
	}
}

// workingCopyUpstream returns the working-copy owner's base URL. There is no
// fallback: the legacy per-service entries were removed from platform.yaml
// with the services they named, so an unset services.runtime surfaces as proxy
// errors rather than traffic silently routed elsewhere.
func workingCopyUpstream(cfg *config.Config) string {
	return cfg.Services.RuntimeServiceURL
}
