// Package handler — Agent Service SSE handler.
//
// All other Agent Service routes (session CRUD, chat, approve, cancel) are
// pure passthrough and handled by GenericProxy with identity path.
// Only AgentSSEEvents remains because it requires SSE streaming with
// context propagation and custom header forwarding.
package handler

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/middleware"
)

// AgentSSEEvents proxies the SSE event stream from Agent Service.
// Forwards user context headers and propagates client disconnect via context.
func AgentSSEEvents(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		sessionID := chi.URLParam(r, "sessionID")

		// Extract user context from middleware-injected values
		userID := middleware.GetUserID(r.Context())
		tenantID := middleware.GetTenantID(r.Context())

		// Build target path
		targetPath := "/api/v1/sessions/" + sessionID + "/events"

		// Forward user context as headers to Agent Service
		extraHeaders := map[string]string{
			"X-User-ID":   userID,
			"X-Tenant-ID": tenantID,
		}

		logger.Info("agent_sse_proxy_start",
			zap.String("session_id", sessionID),
			zap.String("user_id", userID),
		)

		proxySSEWithContext(w, r, cfg.Services.AgentServiceURL, targetPath, extraHeaders, logger)
	}
}
