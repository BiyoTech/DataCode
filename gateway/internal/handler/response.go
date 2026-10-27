// Package handler — JSON response helpers.
//
// Thin wrappers over shared/go/httperr: the envelope shape and the body limit are
// platform contracts (the frontend branches on the error code and receives it
// from both this service and identity-service), so they live in one place rather
// than being spelled out per service. The local names are kept because dozens of
// call sites in this package read better without a package qualifier.
package handler

import (
	"net/http"

	"github.com/datacode/shared/httperr"
)

// decodeJSON parses a JSON body under the platform's size limit, replying with
// 400 on malformed or oversized input. Returns false when a response was written.
func decodeJSON(w http.ResponseWriter, r *http.Request, target interface{}) bool {
	return httperr.DecodeJSON(w, r, target)
}

// writeJSON serializes a payload with the given status code.
func writeJSON(w http.ResponseWriter, status int, payload interface{}) {
	httperr.WriteJSON(w, status, payload)
}

// writeError emits the platform's error envelope: a stable machine-readable
// code plus a message the frontend may show as-is.
func writeError(w http.ResponseWriter, status int, code, message string) {
	httperr.WriteError(w, status, code, message)
}
