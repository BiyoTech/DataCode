// Package handler — generic reverse proxy for transparent request forwarding.
// Instead of writing one handler function per endpoint, GenericProxy handles
// all pure-passthrough routes (GET/POST/PUT/PATCH/DELETE) with a single function.
// Path rewriting (e.g. stripping a route-group prefix) is configurable via ProxyTarget.
package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"go.uber.org/zap"
)

// PathRewriteFunc transforms the incoming gateway request path into the
// backend service path.  This is needed because some route groups use a
// prefix on the gateway side that the backend does not expect
// (e.g. /api/v1/sqlmesh/plan → /api/v1/plan).
type PathRewriteFunc func(gatewayPath string) (backendPath string)

// StatusError is an upstream-resolution error whose HTTP answer is defined by
// the raiser rather than by this package.
//
// Routing failures default to 503 ("workspace temporarily unavailable") because
// the usual cause is a lease that cannot be resolved right now. But some
// failures are CONTRACT verdicts — a 429 LEASE_UNAVAILABLE capacity refusal
// carries a Retry-After the client must honour — and flattening those into the
// generic 503 would strip the one piece of information that makes the error
// actionable. The interface is satisfied structurally, so the leaserouter (or
// any future resolver) can raise typed errors without importing this package.
type StatusError interface {
	error
	// HTTPStatus is the status the client should receive.
	HTTPStatus() int
	// ErrorCode is the platform contract error_code (design 08 §7).
	ErrorCode() string
	// RetryAfterValue is the Retry-After header value, "" when none.
	RetryAfterValue() string
}

// ErrorBody is the optional richer payload a StatusError may provide. When the
// raiser implements it, its keys are merged into the response body alongside
// error_code; otherwise a generic message is derived from Error().
type ErrorBody interface {
	ErrorBody() map[string]any
}

// writeResolutionError answers a failed upstream resolution with the raiser's
// verdict when it has one, and with the generic 503 otherwise.
func writeResolutionError(w http.ResponseWriter, err error, logger *zap.Logger) {
	var verdict StatusError
	if errors.As(err, &verdict) {
		body := map[string]any{
			"error_code": verdict.ErrorCode(),
			"message":    verdict.Error(),
		}
		if richer, ok := verdict.(ErrorBody); ok {
			for key, value := range richer.ErrorBody() {
				body[key] = value
			}
		}
		if retryAfter := verdict.RetryAfterValue(); retryAfter != "" {
			w.Header().Set("Retry-After", retryAfter)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(verdict.HTTPStatus())
		_ = json.NewEncoder(w).Encode(body)
		return
	}
	logger.Warn("proxy_upstream_unresolved", zap.Error(err))
	http.Error(w,
		`{"error":"workspace is temporarily unavailable, please retry"}`,
		http.StatusServiceUnavailable)
}

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
	// ResolveUpstream optionally picks the upstream PER REQUEST, overriding
	// BaseURL when it returns a non-empty address.
	//
	// This exists for working-copy routes (design 06 P4a): a working copy is
	// owned by exactly ONE workspace-runtime replica, and load-balancing a
	// request for it would put a second writer on the same Git worktree. The
	// hook returns an error when no owner can be determined, which must be
	// surfaced rather than papered over with BaseURL — falling back to the
	// load-balanced Service is precisely the corruption being prevented.
	ResolveUpstream func(*http.Request) (string, error)
	// OnForwardFailure is called when a routing decision proved stale: either
	// the upstream could not be reached (pod gone) or it answered
	// 409 LEASE_NOT_HELD (lease moved within the cache TTL). In both cases the
	// cached decision must be invalidated instead of being retried for the
	// rest of its cache lifetime.
	OnForwardFailure func(*http.Request)
}

// upstreamFor picks the base URL for one request.
func (t ProxyTarget) upstreamFor(r *http.Request) (string, error) {
	if t.ResolveUpstream == nil {
		return t.BaseURL, nil
	}
	resolved, err := t.ResolveUpstream(r)
	if err != nil {
		return "", err
	}
	if resolved == "" {
		return t.BaseURL, nil
	}
	return resolved, nil
}

// notifyForwardFailure runs the target's stale-route invalidation hook, if any:
// the upstream chosen for this request could not be reached, so a cached
// routing decision (a lease pointing at a pod that is gone) must not keep
// being retried for the rest of its cache lifetime.
func (t ProxyTarget) notifyForwardFailure(r *http.Request) {
	if t.OnForwardFailure != nil {
		t.OnForwardFailure(r)
	}
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
		baseURL, err := target.upstreamFor(r)
		if err != nil {
			// No owner could be determined. Answering 503 is deliberate for
			// plain resolution failures: forwarding to an arbitrary replica
			// would break the single-writer invariant the lease exists to
			// hold. Contract verdicts (StatusError) keep their own status.
			writeResolutionError(w, err, logger)
			return
		}

		backendPath := rewrite(r.URL.Path)
		if r.URL.RawQuery != "" {
			backendPath += "?" + r.URL.RawQuery
		}

		dispatchWithStaleRouteRetry(w, r, target, baseURL, backendPath, logger)
	}
}
