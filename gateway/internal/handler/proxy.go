// Package handler - HTTP proxy helpers for forwarding requests to backend services.
package handler

import (
	"bytes"
	"io"
	"net/http"
	"strings"
	"time"

	"go.uber.org/zap"
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

// proxyGet forwards a GET request to the target service and writes the response.
func proxyGet(w http.ResponseWriter, _ *http.Request, baseURL, path string, logger *zap.Logger) {
	targetURL := baseURL + path

	resp, err := clientForPath(path).Get(targetURL)
	if err != nil {
		logger.Error("proxy_get_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy response headers
	for k, v := range resp.Header {
		for _, val := range v {
			w.Header().Add(k, val)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxyPost forwards a POST request with body to the target service.
// The original request's Content-Type is preserved so that multipart/form-data
// (file uploads) and other non-JSON content types are forwarded correctly.
func proxyPost(w http.ResponseWriter, r *http.Request, baseURL, path string, body []byte, logger *zap.Logger) {
	targetURL := baseURL + path

	contentType := r.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}

	resp, err := clientForPath(path).Post(targetURL, contentType, bytes.NewReader(body))
	if err != nil {
		logger.Error("proxy_post_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy response headers
	for k, v := range resp.Header {
		for _, val := range v {
			w.Header().Add(k, val)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxyPut forwards a PUT request with body to the target service.
// The original request's Content-Type is preserved for non-JSON payloads.
func proxyPut(w http.ResponseWriter, r *http.Request, baseURL, path string, body []byte, logger *zap.Logger) {
	targetURL := baseURL + path

	req, err := http.NewRequest(http.MethodPut, targetURL, bytes.NewReader(body))
	if err != nil {
		logger.Error("proxy_put_build_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}
	contentType := r.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}
	req.Header.Set("Content-Type", contentType)

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("proxy_put_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, v := range resp.Header {
		for _, val := range v {
			w.Header().Add(k, val)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxyPatch forwards a PATCH request with optional body to the target service.
// The original request's Content-Type is preserved for non-JSON payloads.
func proxyPatch(w http.ResponseWriter, r *http.Request, baseURL, path string, body []byte, logger *zap.Logger) {
	targetURL := baseURL + path

	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}

	req, err := http.NewRequest(http.MethodPatch, targetURL, bodyReader)
	if err != nil {
		logger.Error("proxy_patch_build_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}
	contentType := r.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/json"
	}
	req.Header.Set("Content-Type", contentType)

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("proxy_patch_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, v := range resp.Header {
		for _, val := range v {
			w.Header().Add(k, val)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxySSE forwards a GET request as a Server-Sent Events stream.
// It streams the response body directly to the client without buffering.
func proxySSE(w http.ResponseWriter, _ *http.Request, baseURL, path string, logger *zap.Logger) {
	targetURL := baseURL + path

	req, err := http.NewRequest(http.MethodGet, targetURL, nil)
	if err != nil {
		logger.Error("proxy_sse_build_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}
	req.Header.Set("Accept", "text/event-stream")

	resp, err := sseClient.Do(req)
	if err != nil {
		logger.Error("proxy_sse_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
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

// proxySSEWithContext forwards a GET request as SSE stream with context
// cancellation and custom header forwarding. When the client disconnects,
// the upstream request is automatically cancelled via context propagation.
func proxySSEWithContext(w http.ResponseWriter, r *http.Request, baseURL, path string, extraHeaders map[string]string, logger *zap.Logger) {
	targetURL := baseURL + path

	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, targetURL, nil)
	if err != nil {
		logger.Error("proxy_sse_ctx_build_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}
	req.Header.Set("Accept", "text/event-stream")

	// Forward extra headers (e.g. user context)
	for k, v := range extraHeaders {
		req.Header.Set(k, v)
	}

	resp, err := sseClient.Do(req)
	if err != nil {
		logger.Error("proxy_sse_ctx_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Set SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(resp.StatusCode)

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

// proxyDelete forwards a DELETE request to the target service.
func proxyDelete(w http.ResponseWriter, _ *http.Request, baseURL, path string, logger *zap.Logger) {
	targetURL := baseURL + path

	req, err := http.NewRequest(http.MethodDelete, targetURL, nil)
	if err != nil {
		logger.Error("proxy_delete_build_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}

	resp, err := clientForPath(path).Do(req)
	if err != nil {
		logger.Error("proxy_delete_failed", zap.String("url", targetURL), zap.Error(err))
		http.Error(w, `{"error":"upstream service unavailable"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy response headers
	for k, v := range resp.Header {
		for _, val := range v {
			w.Header().Add(k, val)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}
