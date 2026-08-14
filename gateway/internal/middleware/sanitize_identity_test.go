package middleware

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"go.uber.org/zap"
)

// captureRequest runs SanitizeIdentity and returns what the next handler saw.
func captureRequest(t *testing.T, req *http.Request, userID string) (string, string) {
	t.Helper()

	if userID != "" {
		req = req.WithContext(context.WithValue(req.Context(), UserIDKey, userID))
	}

	var (
		seenBody  string
		seenQuery string
	)
	handler := SanitizeIdentity(zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Fatalf("read forwarded body: %v", err)
			}
			seenBody = string(body)
			seenQuery = r.URL.RawQuery
			w.WriteHeader(http.StatusOK)
		}))
	handler.ServeHTTP(httptest.NewRecorder(), req)
	return seenBody, seenQuery
}

func TestSanitizeIdentityOverwritesBodyUserID(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/git/push",
		strings.NewReader(`{"workspace_path":"/w","user_id":"victim"}`))
	req.Header.Set("Content-Type", "application/json")

	body, _ := captureRequest(t, req, "real-user")

	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		t.Fatalf("forwarded body is not JSON: %v", err)
	}
	if payload["user_id"] != "real-user" {
		t.Errorf("user_id = %v, want %q", payload["user_id"], "real-user")
	}
	// Unrelated fields must survive untouched.
	if payload["workspace_path"] != "/w" {
		t.Errorf("workspace_path = %v, want %q", payload["workspace_path"], "/w")
	}
}

func TestSanitizeIdentityDoesNotAddMissingField(t *testing.T) {
	// Strict downstream schemas must not receive unexpected keys.
	req := httptest.NewRequest(http.MethodPost, "/api/v1/sqlmesh/plan",
		strings.NewReader(`{"environment":"dev"}`))
	req.Header.Set("Content-Type", "application/json")

	body, _ := captureRequest(t, req, "real-user")

	if strings.Contains(body, "user_id") {
		t.Errorf("body gained a user_id field: %s", body)
	}
	if !strings.Contains(body, `"environment":"dev"`) {
		t.Errorf("original payload was altered: %s", body)
	}
}

func TestSanitizeIdentityOverwritesQueryUserID(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet,
		"/api/v1/git/credentials?user_id=victim&provider=github", nil)

	_, query := captureRequest(t, req, "real-user")

	values, err := url.ParseQuery(query)
	if err != nil {
		t.Fatalf("parse forwarded query: %v", err)
	}
	if values.Get("user_id") != "real-user" {
		t.Errorf("user_id = %q, want %q", values.Get("user_id"), "real-user")
	}
	if values.Get("provider") != "github" {
		t.Errorf("provider = %q, want %q", values.Get("provider"), "github")
	}
}

func TestSanitizeIdentityLeavesNonJSONBodyIntact(t *testing.T) {
	const raw = "not-json-payload"
	req := httptest.NewRequest(http.MethodPost, "/api/v1/files/write", strings.NewReader(raw))

	body, _ := captureRequest(t, req, "real-user")

	if body != raw {
		t.Errorf("body = %q, want %q", body, raw)
	}
}

func TestSanitizeIdentitySkipsUnauthenticatedRequests(t *testing.T) {
	// Public routes have no identity, so the payload passes through as-is.
	const raw = `{"user_id":"anything"}`
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", strings.NewReader(raw))

	body, _ := captureRequest(t, req, "")

	if body != raw {
		t.Errorf("body = %q, want %q", body, raw)
	}
}

func TestSanitizeIdentityKeepsContentLengthConsistent(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/git/pull",
		strings.NewReader(`{"user_id":"short"}`))
	req = req.WithContext(context.WithValue(req.Context(), UserIDKey, "a-much-longer-user-id"))

	var forwardedLength int64
	var bodyLength int
	SanitizeIdentity(zap.NewNop())(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			bodyLength = len(body)
			forwardedLength = r.ContentLength
		})).ServeHTTP(httptest.NewRecorder(), req)

	if forwardedLength != int64(bodyLength) {
		t.Errorf("ContentLength = %d, want %d", forwardedLength, bodyLength)
	}
}
