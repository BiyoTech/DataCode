// Package handler — stale-route retry for lease-aware proxies.
//
// The lease contract (design 08 §7) gives the runtime a way to tell the
// gateway its cached route is wrong: answer “409 LEASE_NOT_HELD“. The
// runtime emits it from the request-path ownership check (see
// workspace-runtime “src/api/deps.py“) when a request lands on a replica
// that does not hold the working copy's lease — which happens inside the
// gateway's route-cache TTL whenever a lease moves (holder drained, crashed,
// released).
//
// Only the gateway can act on that verdict: the client cannot fix a stale
// route, so relaying the 409 would surface a platform hand-over as a
// user-facing error. The reaction is fixed by the contract — drop the cached
// route, re-resolve the lease, forward once more.
package handler

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"

	"go.uber.org/zap"

	"github.com/datacode/shared/httperr"
)

// leaseNotHeldCode is the contract error code for "this runtime does not hold
// the requested working copy's lease". The literal is frozen by design 08 §7
// and produced by the shared contract module on the Python side
// (datacode_common.errors.PlatformErrorCode.LEASE_NOT_HELD); both sides must
// agree on it or the retry below silently never triggers.
const leaseNotHeldCode = "LEASE_NOT_HELD"

// conflictBodyLimit bounds how much of a 409 body is buffered for inspection.
// Error payloads are small JSON documents; the limit only exists so a
// misbehaving upstream cannot turn the peek into a memory sink.
const conflictBodyLimit = 64 << 10

// conflictReply is a withheld 409: received, buffered, not yet relayed.
type conflictReply struct {
	statusCode int
	header     http.Header
	body       []byte
	errorCode  string
}

// writeTo relays the withheld reply unchanged (Content-Length is recomputed
// by the writer, like writeUpstream does).
func (c *conflictReply) writeTo(w http.ResponseWriter) {
	for key, values := range c.header {
		if key == "Content-Length" {
			continue
		}
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	w.WriteHeader(c.statusCode)
	w.Write(c.body)
}

// errorCodeOf extracts “error_code“ from a platform error body, or "".
//
// Tolerant by design: a body that is not the platform error shape (empty,
// HTML from a misrouted proxy, truncated JSON) simply has no verdict, and no
// verdict means "relay as-is" — never "guess a retry".
func errorCodeOf(body []byte) string {
	var payload struct {
		ErrorCode string `json:"error_code"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return ""
	}
	return payload.ErrorCode
}

// dispatchWithStaleRouteRetry forwards the request through the target,
// retrying exactly once on a LEASE_NOT_HELD verdict.
//
// Method handling mirrors what the per-method proxy helpers did before:
// GET/DELETE carry no body, POST/PUT/PATCH forward the buffered request body
// (with the caller's Content-Type preserved for multipart uploads), and any
// other method is a 405. Transport failures write a 502 and trigger the
// target's stale-route invalidation hook, exactly as before.
func dispatchWithStaleRouteRetry(
	w http.ResponseWriter,
	r *http.Request,
	target ProxyTarget,
	baseURL string,
	backendPath string,
	logger *zap.Logger,
) {
	var body []byte
	switch r.Method {
	case http.MethodGet, http.MethodDelete:
	case http.MethodPost, http.MethodPut, http.MethodPatch:
		buffered, err := io.ReadAll(r.Body)
		r.Body.Close()
		if err != nil {
			logger.Warn("proxy_read_body_failed",
				zap.String("method", r.Method),
				zap.String("path", r.URL.Path),
				zap.Error(err))
			http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
			return
		}
		body = buffered
	default:
		http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}

	forwardWithStaleRouteRetry(w, r, target, baseURL, backendPath, body, logger)
}

// forwardWithStaleRouteRetry performs up to two attempts.
//
// The retry bound is ONE and it is a contract decision, not a tuning knob: a
// LEASE_NOT_HELD verdict means the cached route is stale, and a single
// re-resolution either finds the new holder or the lease subsystem itself is
// degraded — in which case more attempts would turn a routing hiccup into a
// request storm against Redis. A second verdict is relayed to the client
// verbatim: at that point it is a genuine fault worth surfacing, not a
// hand-over in progress.
func forwardWithStaleRouteRetry(
	w http.ResponseWriter,
	r *http.Request,
	target ProxyTarget,
	baseURL string,
	backendPath string,
	body []byte,
	logger *zap.Logger,
) {
	current := baseURL
	for attempt := 0; ; attempt++ {
		relayed, conflict := sendAttempt(w, r, current, backendPath, body, logger)
		if relayed {
			return
		}
		if conflict == nil {
			// Transport failure: a 502 was already written, and the cached
			// route may point at a pod that is gone — invalidate it.
			target.notifyForwardFailure(r)
			return
		}
		if conflict.errorCode != leaseNotHeldCode || attempt > 0 {
			conflict.writeTo(w)
			return
		}

		// Stale route: invalidate the cached decision and re-resolve once.
		target.notifyForwardFailure(r)
		fresh, err := target.upstreamFor(r)
		if err != nil {
			// Resolution refused (single-writer invariant cannot be
			// upheld): relay the runtime's verdict rather than inventing a
			// different error.
			conflict.writeTo(w)
			return
		}
		logger.Info("proxy_stale_route_retry",
			zap.String("method", r.Method),
			zap.String("path", r.URL.Path),
			zap.String("stale_upstream", current),
			zap.String("fresh_upstream", fresh))
		current = fresh
	}
}

// sendAttempt performs exactly one upstream request.
//
// Everything that is NOT a 409 is relayed immediately (streaming, never
// buffered — successful responses may be file downloads or long documents).
// A 409 is withheld instead: it is the one status whose body the caller must
// read before deciding whether it should reach the client at all, and error
// bodies are small JSON.
//
// Three outcomes:
//
//	(true,  nil)   written to the client (streamed reply, or a build-error 500)
//	(false, nil)   transport failure — a 502 was written; caller invalidates
//	(false, reply) 409 withheld — caller inspects and either retries or relays
func sendAttempt(
	w http.ResponseWriter,
	r *http.Request,
	baseURL string,
	backendPath string,
	body []byte,
	logger *zap.Logger,
) (bool, *conflictReply) {
	targetURL := baseURL + backendPath

	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(r.Method, targetURL, reader)
	if err != nil {
		logger.Error("proxy_request_build_failed",
			zap.String("method", r.Method),
			zap.String("url", targetURL),
			zap.Error(err))
		httperr.WriteInternalError(w)
		return true, nil
	}
	if body != nil {
		req.Header.Set("Content-Type", contentTypeOf(r))
	}
	forwardIdentity(req, r)

	resp, err := clientForPath(backendPath).Do(req)
	if err != nil {
		logger.Error("proxy_forward_failed",
			zap.String("method", r.Method),
			zap.String("url", targetURL),
			zap.Error(err))
		httperr.WriteUpstreamUnavailable(w)
		return false, nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusConflict {
		relayResponse(w, resp)
		return true, nil
	}

	payload, readErr := io.ReadAll(io.LimitReader(resp.Body, conflictBodyLimit))
	reply := &conflictReply{
		statusCode: resp.StatusCode,
		header:     resp.Header.Clone(),
		body:       payload,
		errorCode:  errorCodeOf(payload),
	}
	if readErr != nil {
		// The body could not be fully read; relay what was captured. This
		// cannot be a trustworthy verdict, and errorCodeOf already saw only
		// complete JSON, so the retry path stays honest.
		reply.errorCode = ""
	}
	return false, reply
}
