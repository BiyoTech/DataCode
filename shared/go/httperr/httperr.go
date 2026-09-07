// Package httperr is the platform's HTTP response vocabulary for the Go
// services: the error envelope, the request-body limit, and the bearer-token
// form of the Authorization header.
//
// Why this is shared rather than duplicated per service: the gateway and
// identity-service both answer the same frontend, so an error the frontend must
// branch on has to arrive in the same shape from either of them. Before this
// package existed the envelope was written twice (byte-identical) while the code
// used for one condition had already drifted — "Malformed request body." was
// reported as `invalid_body` in two places and `invalid_request` in a third, so a
// client that keyed on the code saw a different one depending on which handler it
// hit.
//
// The upstream-failure responses live here for the same reason: the gateway wrote
// them from seventeen call sites, and a proxy that reports "internal error" where
// it means "the upstream is unreachable" costs an on-call engineer the first
// twenty minutes of every incident.
package httperr

import (
	"encoding/json"
	"net/http"
	"strings"
)

// MaxRequestBodyBytes bounds a decoded JSON request body.
//
// The endpoints that decode a body exchange small documents (credentials,
// preferences, role compositions); file content travels through the streaming
// proxies, which never decode. An unbounded decoder lets one request hold as
// much memory as the client cares to send.
const MaxRequestBodyBytes = 1 << 16 // 64 KiB

// Stable machine-readable error codes. The frontend branches on these, so the
// literals are part of the API contract and must not be reworded.
const (
	// CodeInvalidBody marks a body that is absent, malformed or oversized.
	CodeInvalidBody = "invalid_body"
	// CodeInternalError marks a fault inside the platform.
	CodeInternalError = "internal_error"
	// CodeUpstreamUnavailable marks a healthy gateway that could not reach the
	// service behind it — deliberately distinct from CodeInternalError, because
	// the two lead to completely different diagnoses.
	CodeUpstreamUnavailable = "upstream_unavailable"
)

// Human-readable messages the frontend may show as-is.
const (
	MsgInvalidBody         = "Malformed request body."
	MsgInternalError       = "Unexpected error. Please retry."
	MsgUpstreamUnavailable = "Upstream service unavailable. Please retry."
)

const bearerPrefix = "bearer "

// WriteJSON serializes a payload with the given status code.
func WriteJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// WriteError emits the platform's error envelope: a stable machine-readable code
// plus a message the frontend may show as-is.
func WriteError(w http.ResponseWriter, status int, code, message string) {
	WriteJSON(w, status, map[string]string{"code": code, "message": message})
}

// WriteInternalError reports a fault inside the platform.
func WriteInternalError(w http.ResponseWriter) {
	WriteError(w, http.StatusInternalServerError, CodeInternalError, MsgInternalError)
}

// WriteUpstreamUnavailable reports that a service behind this one could not be
// reached. 502, never 500: the distinction is what tells an operator whether to
// look at this process or at the one it proxies to.
func WriteUpstreamUnavailable(w http.ResponseWriter) {
	WriteError(w, http.StatusBadGateway, CodeUpstreamUnavailable, MsgUpstreamUnavailable)
}

// DecodeJSON parses a JSON body under MaxRequestBodyBytes, replying with 400 on
// malformed or oversized input. Returns false when a response was written.
func DecodeJSON(w http.ResponseWriter, r *http.Request, target any) bool {
	reader := http.MaxBytesReader(w, r.Body, MaxRequestBodyBytes)
	if err := json.NewDecoder(reader).Decode(target); err != nil {
		WriteError(w, http.StatusBadRequest, CodeInvalidBody, MsgInvalidBody)
		return false
	}
	return true
}

// BearerToken extracts the token from an "Authorization: Bearer <token>" header,
// or returns "" when the header is absent or differently shaped.
//
// The scheme match is case-insensitive because RFC 7235 says it is, and clients
// in the wild send "bearer".
func BearerToken(header string) string {
	value := strings.TrimSpace(header)
	if len(value) < len(bearerPrefix) {
		return ""
	}
	if !strings.EqualFold(value[:len(bearerPrefix)], bearerPrefix) {
		return ""
	}
	return strings.TrimSpace(value[len(bearerPrefix):])
}
