package httperr

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func decodedBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]string {
	t.Helper()
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not a JSON object: %v (%q)", err, rec.Body.String())
	}
	return body
}

func TestWriteErrorEnvelope(t *testing.T) {
	rec := httptest.NewRecorder()
	WriteError(rec, http.StatusForbidden, "denied", "Nope.")

	if rec.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Errorf("content type = %q, want application/json", got)
	}
	body := decodedBody(t, rec)
	if body["code"] != "denied" || body["message"] != "Nope." {
		t.Errorf("body = %v, want code/message pair", body)
	}
}

// TestUpstreamFailureIsDistinctFromInternalFault pins the distinction the
// gateway's seventeen hand-written responses kept blurring: 502 means "the
// service behind me is unreachable", 500 means "I am broken". Collapsing them
// costs an operator the first minutes of an incident.
func TestUpstreamFailureIsDistinctFromInternalFault(t *testing.T) {
	upstream := httptest.NewRecorder()
	WriteUpstreamUnavailable(upstream)
	if upstream.Code != http.StatusBadGateway {
		t.Errorf("upstream failure status = %d, want 502", upstream.Code)
	}

	internal := httptest.NewRecorder()
	WriteInternalError(internal)
	if internal.Code != http.StatusInternalServerError {
		t.Errorf("internal fault status = %d, want 500", internal.Code)
	}

	if decodedBody(t, upstream)["code"] == decodedBody(t, internal)["code"] {
		t.Error("the two conditions must not share an error code")
	}
}

func TestDecodeJSONAcceptsValidBody(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"name":"alice"}`))
	rec := httptest.NewRecorder()

	var target struct{ Name string }
	if !DecodeJSON(rec, req, &target) {
		t.Fatalf("valid body rejected: %s", rec.Body.String())
	}
	if target.Name != "alice" {
		t.Errorf("decoded name = %q, want alice", target.Name)
	}
}

func TestDecodeJSONRejectsMalformedBody(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{not json`))
	rec := httptest.NewRecorder()

	var target struct{ Name string }
	if DecodeJSON(rec, req, &target) {
		t.Fatal("malformed body accepted")
	}
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
	if code := decodedBody(t, rec)["code"]; code != CodeInvalidBody {
		t.Errorf("code = %q, want %q — the frontend branches on it", code, CodeInvalidBody)
	}
}

// TestDecodeJSONRejectsOversizedBody is the regression guard for identity's three
// unbounded decoders: without the limit, one request holds as much memory as the
// client chooses to send.
func TestDecodeJSONRejectsOversizedBody(t *testing.T) {
	oversized := `{"name":"` + strings.Repeat("a", MaxRequestBodyBytes+1) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(oversized))
	rec := httptest.NewRecorder()

	var target struct{ Name string }
	if DecodeJSON(rec, req, &target) {
		t.Fatal("oversized body accepted")
	}
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestBearerToken(t *testing.T) {
	cases := map[string]string{
		"Bearer abc123":    "abc123",
		"bearer abc123":    "abc123", // RFC 7235: the scheme is case-insensitive.
		"BEARER  abc123  ": "abc123",
		"Basic abc123":     "",
		"abc123":           "",
		"":                 "",
		"Bearer":           "",
		"Bearer ":          "",
	}
	for header, want := range cases {
		if got := BearerToken(header); got != want {
			t.Errorf("BearerToken(%q) = %q, want %q", header, got, want)
		}
	}
}
