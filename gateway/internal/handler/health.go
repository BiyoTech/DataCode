// Package handler — local (non-proxy) handlers.
package handler

import (
	"encoding/json"
	"net/http"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/config"
)

type upstreamHealth struct {
	Status string `json:"status"`
	URL    string `json:"url,omitempty"`
	Error  string `json:"error,omitempty"`
}

// HealthCheck returns gateway liveness. With ?deep=1, probes upstream services.
func HealthCheck(cfg *config.Config, logger *zap.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")

		if r.URL.Query().Get("deep") != "1" {
			json.NewEncoder(w).Encode(map[string]string{
				"status":  "healthy",
				"service": "gateway",
			})
			return
		}

		upstreams := map[string]string{
			"agent":   cfg.Services.AgentServiceURL + "/health",
			"runtime": cfg.Services.RuntimeServiceURL + "/health",
			"control": controlPlaneURL(cfg) + "/health",
		}

		results := make(map[string]upstreamHealth, len(upstreams))
		overall := "healthy"

		for name, url := range upstreams {
			client := &http.Client{Timeout: 5 * time.Second}
			resp, err := client.Get(url)
			if err != nil {
				overall = "degraded"
				results[name] = upstreamHealth{Status: "unavailable", URL: url, Error: err.Error()}
				continue
			}
			resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				results[name] = upstreamHealth{Status: "healthy", URL: url}
			} else {
				overall = "degraded"
				results[name] = upstreamHealth{
					Status: "unhealthy",
					URL:    url,
					Error:  resp.Status,
				}
			}
		}

		if overall != "healthy" {
			logger.Warn("deep_health_degraded", zap.Any("upstreams", results))
		}

		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":    overall,
			"service":   "gateway",
			"upstreams": results,
		})
	}
}
