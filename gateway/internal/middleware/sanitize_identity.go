package middleware

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"

	"go.uber.org/zap"
)

// maxSanitizedBodyBytes caps how much of a request body is buffered for
// identity sanitization. Larger bodies are forwarded untouched — they are not
// identity-bearing JSON payloads.
const maxSanitizedBodyBytes = 1 << 20 // 1 MiB

// SanitizeIdentity rewrites identity fields that clients must not control.
//
// Downstream services still receive `user_id` / `tenant_id` in bodies and query
// strings (their existing contract), but the values are replaced with the
// identity proven by the access token. Only fields that are already present are
// rewritten, so no unexpected keys are introduced into strict schemas.
func SanitizeIdentity(logger *zap.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			identity := map[string]string{
				"user_id":   GetUserID(r.Context()),
				"tenant_id": GetTenantID(r.Context()),
			}
			if identity["user_id"] == "" {
				// Public routes carry no identity to enforce.
				next.ServeHTTP(w, r)
				return
			}

			sanitizeQuery(r, identity, logger)
			sanitizeBody(r, identity, logger)
			next.ServeHTTP(w, r)
		})
	}
}

// sanitizeQuery overwrites identity query parameters with the verified values.
func sanitizeQuery(r *http.Request, identity map[string]string, logger *zap.Logger) {
	query := r.URL.Query()
	changed := false
	for field, verified := range identity {
		current := query.Get(field)
		if verified == "" || current == "" || current == verified {
			continue
		}
		logger.Warn("request_identity_overridden",
			zap.String("path", r.URL.Path),
			zap.String("location", "query"),
			zap.String("field", field))
		query.Set(field, verified)
		changed = true
	}
	if changed {
		r.URL.RawQuery = query.Encode()
	}
}

// sanitizeBody overwrites identity fields inside a JSON body with the verified
// values, leaving every other field untouched.
func sanitizeBody(r *http.Request, identity map[string]string, logger *zap.Logger) {
	if r.Body == nil || r.ContentLength == 0 {
		return
	}
	switch r.Method {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
	default:
		return
	}
	if r.ContentLength > maxSanitizedBodyBytes {
		return
	}

	original, err := io.ReadAll(io.LimitReader(r.Body, maxSanitizedBodyBytes+1))
	r.Body.Close()
	if err != nil || len(original) > maxSanitizedBodyBytes {
		// Restore what was read so the proxy can still forward it.
		restoreBody(r, original)
		return
	}

	var payload map[string]json.RawMessage
	if err := json.Unmarshal(original, &payload); err != nil {
		// Not a JSON object (form data, array, binary): forward unchanged.
		restoreBody(r, original)
		return
	}

	changed := false
	for field, verified := range identity {
		raw, present := payload[field]
		if !present || verified == "" {
			continue
		}
		var current string
		if err := json.Unmarshal(raw, &current); err == nil && current == verified {
			continue
		}
		encoded, err := json.Marshal(verified)
		if err != nil {
			continue
		}
		logger.Warn("request_identity_overridden",
			zap.String("path", r.URL.Path),
			zap.String("location", "body"),
			zap.String("field", field))
		payload[field] = encoded
		changed = true
	}
	if !changed {
		restoreBody(r, original)
		return
	}

	rewritten, err := json.Marshal(payload)
	if err != nil {
		restoreBody(r, original)
		return
	}
	restoreBody(r, rewritten)
}

// restoreBody makes body readable again by downstream handlers and keeps
// Content-Length consistent with the (possibly rewritten) payload.
func restoreBody(r *http.Request, body []byte) {
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.Header.Set("Content-Length", strconv.Itoa(len(body)))
}
