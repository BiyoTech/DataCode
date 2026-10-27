// Package handler - HTTP proxy helpers for forwarding requests to backend services.
package handler

import (
	"bytes"
	"io"
	"net/http"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/shared/httperr"
)

// upstreamClient is a shared HTTP client with timeout for all proxy calls.
var upstreamClient = &http.Client{
	Timeout: 25 * time.Second,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

// longTimeoutClient is used for plan/apply operations that may run for minutes.
var longTimeoutClient = &http.Client{
	Timeout: 300 * time.Second,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

func clientForPath(path string) *http.Client {
	if strings.Contains(path, "/plan") || strings.Contains(path, "/apply") {
		return longTimeoutClient
	}
	return upstreamClient
}

// sseClient is a dedicated HTTP client for SSE streaming with long timeout.
// SSE connections are long-lived; a short timeout would break the stream.
var sseClient = &http.Client{
	Timeout: 0, // No timeout — SSE is a long-lived connection
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

// forwardedHeaders are the identity and capability headers produced by the
// gateway middleware chain. They are the only authorization input downstream
// services are allowed to trust, so they must accompany every proxied request.
//
// Role names are deliberately absent: services authorize on the granted
// permission set, and a role is a tenant-editable composition.
//
// X-Trace-ID is not an authorization input: it is set by RequestLogger and
// carried along so a downstream log line can be correlated with the gateway
// access log for the same request.
var forwardedHeaders = []string{
	"X-User-ID",
	"X-Tenant-ID",
	"X-Username",
	"X-Workspace-ID",
	"X-Granted-Permissions",
	"X-Trace-ID",
}

// forwardIdentity copies the gateway-controlled identity and capability headers
// onto an upstream request.
func forwardIdentity(dst *http.Request, src *http.Request) {
	if src == nil || dst == nil {
		return
	}
	for _, header := range forwardedHeaders {
		if value := src.Header.Get(header); value != "" {
			dst.Header.Set(header, value)
		}
	}
}

// relayResponse copies status and headers of an upstream reply to the client.
func relayResponse(w http.ResponseWriter, resp *http.Response) {
	for key, values := range resp.Header {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxyGet forwards a GET request to the target service and writes the response.
//
// The bool reports whether the upstream was REACHED: false means the transport
// failed after the routing decision, which is the signal a lease-aware target
// uses to invalidate a possibly-stale route (ProxyTarget.OnForwardFailure).
func proxyGet(w http.ResponseWriter, r *http.Request, baseURL, path string, logger *zap.Logger) bool {
	targetURL := baseURL + path

	req, err := http.NewRequest(http.MethodGet, targetURL, nil)
	if err != nil {
		logger.Error("proxy_get_build_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteInternalError(w)
		return true
	}
	forwardIdentity(req, r)

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("proxy_get_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteUpstreamUnavailable(w)
		return false
	}
	defer resp.Body.Close()

	relayResponse(w, resp)
	return true
}

// proxyPost forwards a POST request with body to the target service.
// The original request's Content-Type is preserved so that multipart/form-data
// (file uploads) and other non-JSON content types are forwarded correctly.
// The bool reports whether the upstream was reached (see proxyGet).
func proxyPost(w http.ResponseWriter, r *http.Request, baseURL, path string, body []byte, logger *zap.Logger) bool {
	targetURL := baseURL + path

	req, err := http.NewRequest(http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		logger.Error("proxy_post_build_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteInternalError(w)
		return true
	}
	req.Header.Set("Content-Type", contentTypeOf(r))
	forwardIdentity(req, r)

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("proxy_post_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteUpstreamUnavailable(w)
		return false
	}
	defer resp.Body.Close()

	relayResponse(w, resp)
	return true
}

// contentTypeOf returns the inbound content type, defaulting to JSON.
func contentTypeOf(r *http.Request) string {
	if r == nil {
		return "application/json"
	}
	if contentType := r.Header.Get("Content-Type"); contentType != "" {
		return contentType
	}
	return "application/json"
}

// proxySSE forwards a GET request as a Server-Sent Events stream.
//
// It streams the response body directly to the client without buffering. The
// upstream request carries the client's context, so a client disconnect cancels
// it: the read loop below then ends on the context error instead of holding the
// upstream connection open for a stream nobody is listening to anymore.
//
// extraHeaders are set before the identity headers (e.g. the agent stream's
// per-user routing headers) and may be nil.
func proxySSE(w http.ResponseWriter, r *http.Request, baseURL, path string, extraHeaders map[string]string, logger *zap.Logger) {
	targetURL := baseURL + path

	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, targetURL, nil)
	if err != nil {
		logger.Error("proxy_sse_build_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteInternalError(w)
		return
	}
	req.Header.Set("Accept", "text/event-stream")
	for k, v := range extraHeaders {
		req.Header.Set(k, v)
	}
	forwardIdentity(req, r)

	resp, err := sseClient.Do(req)
	if err != nil {
		logger.Error("proxy_sse_failed", zap.String("url", targetURL), zap.Error(err))
		httperr.WriteUpstreamUnavailable(w)
		return
	}
	defer resp.Body.Close()

	// Set SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(resp.StatusCode)

	// Flush-capable writer for streaming
	flusher, ok := w.(http.Flusher)
	if !ok {
		io.Copy(w, resp.Body)
		return
	}

	buf := make([]byte, 4096)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			w.Write(buf[:n])
			flusher.Flush()
		}
		if readErr != nil {
			break
		}
	}
}

// ---------------------------------------------------------------------------
// Buffered upstream calls
// ---------------------------------------------------------------------------

// upstreamResponse is a fully buffered upstream reply. It is used when the
// gateway must act on the outcome before answering the client — for example to
// record the owner role binding only after a workspace was really created.
type upstreamResponse struct {
	StatusCode int
	Header     http.Header
	Body       []byte
}

// OK reports whether the upstream call succeeded.
func (u *upstreamResponse) OK() bool {
	return u.StatusCode >= 200 && u.StatusCode < 300
}

// callUpstream performs a request against a backend service and buffers the
// whole reply. Bodies handled this way are small JSON documents.
func callUpstream(
	r *http.Request, method, baseURL, path string, body []byte, logger *zap.Logger,
) (*upstreamResponse, error) {
	targetURL := baseURL + path

	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(r.Context(), method, targetURL, reader)
	if err != nil {
		logger.Error("upstream_build_failed", zap.String("url", targetURL), zap.Error(err))
		return nil, err
	}
	contentType := r.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}
	if body != nil {
		req.Header.Set("Content-Type", contentType)
	}
	forwardIdentity(req, r)

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("upstream_call_failed",
			zap.String("method", method), zap.String("url", targetURL), zap.Error(err))
		return nil, err
	}
	defer resp.Body.Close()

	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		logger.Error("upstream_read_failed", zap.String("url", targetURL), zap.Error(err))
		return nil, err
	}
	return &upstreamResponse{
		StatusCode: resp.StatusCode,
		Header:     resp.Header.Clone(),
		Body:       payload,
	}, nil
}

// writeUpstream relays a buffered upstream reply to the client unchanged.
func writeUpstream(w http.ResponseWriter, resp *upstreamResponse) {
	for key, values := range resp.Header {
		// Content-Length is recomputed by the writer.
		if key == "Content-Length" {
			continue
		}
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	w.WriteHeader(resp.StatusCode)
	w.Write(resp.Body)
}
