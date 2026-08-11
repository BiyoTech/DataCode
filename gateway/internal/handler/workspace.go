// Package handler — Workspace and Git credential special handlers that
// require user_id injection from request context.
//
// All pure-passthrough workspace/file/git-panel routes are now handled
// declaratively via GenericProxy / MakeValidatedProxy.
package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/agenticdata/gateway/internal/config"
	"github.com/agenticdata/gateway/internal/middleware"
)

// ---------------------------------------------------------------------------
// Workspace handlers — user_id injection into request body
// ---------------------------------------------------------------------------

// CreateWorkspace proxies workspace creation to SQLMesh Service.
// Injects the authenticated user_id into the request body if not already present.
func CreateWorkspace(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectUserIDIntoBody(cfg, logger, "/api/v1/workspaces")
}

// BindGitRepo proxies Git repository binding to SQLMesh Service.
// Injects the authenticated user_id into the request body if not already present.
func BindGitRepo(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectUserIDIntoBody(cfg, logger, "/api/v1/workspaces/bind-git")
}

// CreateGitCredential proxies Git credential creation to SQLMesh Service.
// Injects the authenticated user_id into the request body if not already present.
func CreateGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return injectUserIDIntoBody(cfg, logger, "/api/v1/git/credentials")
}

// injectUserIDIntoBody is a shared factory for handlers that read a JSON body,
// inject user_id from context (if absent), and POST to the given backend path.
func injectUserIDIntoBody(cfg *config.Config, logger *zap.Logger, backendPath string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())

		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
			return
		}
		defer r.Body.Close()

		var reqBody map[string]interface{}
		if err := json.Unmarshal(body, &reqBody); err != nil {
			http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
			return
		}
		if _, ok := reqBody["user_id"]; !ok {
			reqBody["user_id"] = userID
		}
		body, _ = json.Marshal(reqBody)

		proxyPost(w, r, cfg.Services.SQLMeshServiceURL, backendPath, body, logger)
	}
}

// ---------------------------------------------------------------------------
// Workspace / credential listing — user_id from context as query param
// ---------------------------------------------------------------------------

// ListWorkspaces proxies workspace listing to SQLMesh Service.
// Uses user_id from request context unless explicitly provided in query.
func ListWorkspaces(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := r.URL.Query().Get("user_id")
		if userID == "" {
			userID = middleware.GetUserID(r.Context())
		}
		targetURL := fmt.Sprintf("/api/v1/workspaces?user_id=%s", url.QueryEscape(userID))
		proxyGet(w, r, cfg.Services.SQLMeshServiceURL, targetURL, logger)
	}
}

// ListGitCredentials proxies Git credentials listing to SQLMesh Service.
// Uses user_id from request context.
func ListGitCredentials(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials?user_id=%s", url.QueryEscape(userID))
		proxyGet(w, r, cfg.Services.SQLMeshServiceURL, targetURL, logger)
	}
}

// ---------------------------------------------------------------------------
// Credential management — user_id + path params
// ---------------------------------------------------------------------------

// DeleteGitCredential proxies Git credential deletion to SQLMesh Service.
// Appends user_id from context as a query param for authorization.
func DeleteGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		credentialID := chi.URLParam(r, "credentialID")
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials/%s?user_id=%s",
			url.PathEscape(credentialID), url.QueryEscape(userID))
		proxyDelete(w, r, cfg.Services.SQLMeshServiceURL, targetURL, logger)
	}
}

// SetDefaultGitCredential proxies setting a credential as default to SQLMesh Service.
// Appends user_id from context as a query param for authorization.
func SetDefaultGitCredential(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		credentialID := chi.URLParam(r, "credentialID")
		userID := middleware.GetUserID(r.Context())
		targetURL := fmt.Sprintf("/api/v1/git/credentials/%s/default?user_id=%s",
			url.PathEscape(credentialID), url.QueryEscape(userID))
		proxyPatch(w, r, cfg.Services.SQLMeshServiceURL, targetURL, nil, logger)
	}
}
