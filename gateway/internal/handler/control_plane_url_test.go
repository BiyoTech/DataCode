package handler

import (
	"testing"

	"github.com/datacode/gateway/internal/config"
)

// TestControlPlaneURL pins the P5 routing contract for the metadata surface:
// workspace management, the environment registry and Git credentials belong
// to control-service, and there is NO fallback — the legacy per-service
// entries were removed from platform.yaml with the services they named, so an
// unset services.control surfaces as proxy errors, never as traffic silently
// routed somewhere else.
func TestControlPlaneURL(t *testing.T) {
	cfg := &config.Config{
		Services: config.ServicesConfig{
			ControlServiceURL: "http://control:8004",
		},
	}
	if got := controlPlaneURL(cfg); got != "http://control:8004" {
		t.Errorf("controlPlaneURL = %q, want the control-service URL", got)
	}
}

// TestWorkingCopyUpstream pins the SSE streams' upstream: the working-copy
// owner (workspace-runtime), never the control plane and never a retired
// per-service entry.
func TestWorkingCopyUpstream(t *testing.T) {
	cfg := &config.Config{
		Services: config.ServicesConfig{
			RuntimeServiceURL: "http://runtime:8002",
			ControlServiceURL: "http://control:8004",
		},
	}
	if got := workingCopyUpstream(cfg); got != "http://runtime:8002" {
		t.Errorf("workingCopyUpstream = %q, want the runtime URL", got)
	}
}
