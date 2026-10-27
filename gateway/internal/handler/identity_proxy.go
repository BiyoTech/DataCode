// Package handler — reverse proxy for the identity surface.
//
// Why /auth, /me and /rbac are proxied rather than served locally (design 06
// P2, P7 prerequisite):
//
// identity-service is the single source of truth for identity and
// authorization. Serving these endpoints from gateway code required a second
// copy of the users/roles tables inside the gateway, which made the identity
// dataset have two writers and blocked the P7 Global/Cell database split.
// Forwarding removes the copy without changing the public API — the paths,
// payloads and status codes are exactly the ones identity-service emits.
//
// Difference from GenericProxy: this proxy forwards the `Authorization` header.
// identity-service verifies the JWT itself (it must, for the endpoints that are
// public at the gateway, such as login and refresh, where no verified identity
// exists yet). Everything else the gateway guarantees stays in force: the
// Authorize middleware has already run, and the forged-header stripping in
// JWTAuth/Authorize applies to this route group like any other.
package handler

import (
	"io"
	"net/http"
	"strings"

	"go.uber.org/zap"

	"github.com/datacode/shared/httperr"
)

// identityForwardedHeaders are the request headers an identity call needs on
// top of the standard gateway identity set.
//
// Authorization: identity-service authenticates the caller itself.
// Content-Type: preserved so a JSON body is parsed as such.
var identityForwardedHeaders = []string{"Authorization", "Content-Type"}

// apiPrefix is the public namespace the gateway owns. identity-service mounts
// its routes WITHOUT it (`/auth/login`, not `/api/v1/auth/login`), because the
// prefix is a gateway concern — so it is stripped on the way through.
const apiPrefix = "/api/v1"

// identityBackendPath converts a gateway request path into the identity-service
// path, stripping the gateway's public API prefix.
func identityBackendPath(gatewayPath string) string {
	trimmed := strings.TrimPrefix(gatewayPath, apiPrefix)
	if trimmed == "" {
		return "/"
	}
	if !strings.HasPrefix(trimmed, "/") {
		// The path did not carry the prefix at all; forward it unchanged.
		return gatewayPath
	}
	return trimmed
}

// IdentityProxy forwards a request to identity-service unchanged.
//
// baseURL empty means the deployment has no identity-service configured. That
// is fatal for these routes rather than degradable: authentication cannot be
// served from anywhere else, so the handler answers 503 instead of silently
// falling back to a local implementation that no longer exists.
func IdentityProxy(baseURL string, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if baseURL == "" {
			logger.Error("identity_proxy_unconfigured",
				zap.String("path", r.URL.Path),
				zap.String("hint", "set services.identity_service_url"))
			http.Error(w,
				`{"code":"identity_unavailable","message":"Identity service is not configured."}`,
				http.StatusServiceUnavailable)
			return
		}

		targetPath := identityBackendPath(r.URL.Path)
		if r.URL.RawQuery != "" {
			targetPath += "?" + r.URL.RawQuery
		}

		req, err := http.NewRequestWithContext(r.Context(), r.Method, baseURL+targetPath, r.Body)
		if err != nil {
			logger.Error("identity_proxy_build_failed",
				zap.String("path", r.URL.Path), zap.Error(err))
			httperr.WriteInternalError(w)
			return
		}
		forwardIdentity(req, r)
		for _, header := range identityForwardedHeaders {
			if value := r.Header.Get(header); value != "" {
				req.Header.Set(header, value)
			}
		}

		resp, err := upstreamClient.Do(req)
		if err != nil {
			logger.Error("identity_proxy_failed",
				zap.String("method", r.Method),
				zap.String("path", r.URL.Path), zap.Error(err))
			http.Error(w,
				`{"code":"identity_unavailable","message":"Identity service is unavailable."}`,
				http.StatusBadGateway)
			return
		}
		defer func() { _ = resp.Body.Close() }()

		for key, values := range resp.Header {
			for _, value := range values {
				w.Header().Add(key, value)
			}
		}
		w.WriteHeader(resp.StatusCode)
		_, _ = io.Copy(w, resp.Body)
	}
}
