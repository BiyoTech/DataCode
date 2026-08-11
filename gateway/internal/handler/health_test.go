package handler_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agenticdata/gateway/internal/config"
	"github.com/agenticdata/gateway/internal/handler"
	"go.uber.org/zap"
)

func TestHealthCheckShallow(t *testing.T) {
	cfg := &config.Config{}
	logger := zap.NewNop()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	rec := httptest.NewRecorder()

	handler.HealthCheck(cfg, logger)(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("invalid json: %v", err)
	}
	if body["status"] != "healthy" {
		t.Fatalf("expected healthy, got %q", body["status"])
	}
}

func TestHealthCheckDeepDegradedWhenUpstreamMissing(t *testing.T) {
	cfg := &config.Config{
		Services: config.ServicesConfig{
			AgentServiceURL:    "http://127.0.0.1:1",
			SQLMeshServiceURL:  "http://127.0.0.1:1",
			OntologyServiceURL: "http://127.0.0.1:1",
		},
	}
	logger := zap.NewNop()
	req := httptest.NewRequest(http.MethodGet, "/health?deep=1", nil)
	rec := httptest.NewRecorder()

	handler.HealthCheck(cfg, logger)(rec, req)

	var body map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("invalid json: %v", err)
	}
	if body["status"] != "degraded" {
		t.Fatalf("expected degraded, got %v", body["status"])
	}
}
