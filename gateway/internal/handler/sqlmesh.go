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

	"github.com/agenticdata/gateway/internal/config"
)

// SSEEvents proxies the Server-Sent Events stream from SQLMesh Service.
// Streams events directly to the client without buffering for real-time updates.
func SSEEvents(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		targetPath := "/api/v1/events"
		if ws := r.URL.Query().Get("workspace_path"); ws != "" {
			targetPath += "?workspace_path=" + url.QueryEscape(ws)
		}
		proxySSE(w, r, cfg.Services.SQLMeshServiceURL, targetPath, logger)
	}
}
