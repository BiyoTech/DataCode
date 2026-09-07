package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The Go and Python loaders are two SEPARATE implementations of the same
// base+profile contract. Testing only one side leaves the other free to drift,
// and the drift is silent: the gateway would resolve a different topology than
// the services it routes to, which presents as intermittent 502s rather than a
// configuration error.
//
// These tests therefore mirror shared/python/tests/test_config_profiles.py
// against the SAME shipped files.

// confDir locates the repository's conf/ directory from this package.
func confDir(t *testing.T) string {
	t.Helper()
	// gateway/internal/config -> repository root is three levels up.
	dir, err := filepath.Abs(filepath.Join("..", "..", "..", "conf"))
	if err != nil {
		t.Fatalf("resolve conf dir: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "platform.yaml")); err != nil {
		t.Fatalf("conf/platform.yaml not found at %s: %v", dir, err)
	}
	return dir
}

// loadProfile resolves a shipped profile through the production loader.
func loadProfile(t *testing.T, profile string) *PlatformConfig {
	t.Helper()
	t.Setenv("CONFIG_PATH", confDir(t))
	t.Setenv("DATACODE_ENV", profile)
	return LoadPlatform()
}

var allProfiles = []string{"dev", "docker", "cell", "global", "k8s-cell", "k8s-global"}

// fullPlatformProfiles run every service; the global-tier ones run identity
// alone and legitimately publish no other address.
var fullPlatformProfiles = []string{"dev", "docker", "cell", "k8s-cell"}

// containerProfiles resolve their addresses from INSIDE a container, where
// "localhost" is the container itself rather than the intended peer.
var containerProfiles = []string{"docker", "cell", "global", "k8s-cell", "k8s-global"}

// TestProfilesExist fails when a documented profile has no file: a missing
// profile does not error at load time, it silently leaves the base defaults in
// place.
func TestProfilesExist(t *testing.T) {
	dir := confDir(t)
	for _, profile := range allProfiles {
		path := filepath.Join(dir, fmt.Sprintf("platform.%s.yaml", profile))
		if _, err := os.Stat(path); err != nil {
			t.Errorf("profile %q has no file at %s", profile, path)
		}
	}
}

// TestProfilesResolveDatabase pins that every profile fully addresses its
// database. The Go loader is what identity-service uses to build its DSN, so an
// unaddressed database here is a service that cannot start.
func TestProfilesResolveDatabase(t *testing.T) {
	for _, profile := range allProfiles {
		t.Run(profile, func(t *testing.T) {
			cfg := loadProfile(t, profile)

			if cfg.Database.Type != "postgresql" {
				t.Errorf("database.type = %q, want postgresql (one engine across all environments)", cfg.Database.Type)
			}
			if cfg.Database.Host == "" {
				t.Error("database.host is empty")
			}
			if cfg.Database.Name == "" {
				t.Error("database.name is empty")
			}
		})
	}
}

// TestFullPlatformProfilesPublishEveryServiceURL guards the topology the
// gateway routes on. An empty URL here does not fail at startup — it fails per
// request, as `unsupported protocol scheme ""`.
func TestFullPlatformProfilesPublishEveryServiceURL(t *testing.T) {
	for _, profile := range fullPlatformProfiles {
		t.Run(profile, func(t *testing.T) {
			cfg := loadProfile(t, profile)

			urls := map[string]string{
				"agent":    cfg.AgentServiceURL(),
				"runtime":  cfg.RuntimeServiceURL(),
				"control":  cfg.ControlServiceURL(),
				"identity": cfg.IdentityServiceURL(),
			}
			for name, url := range urls {
				if url == "" {
					t.Errorf("%s URL is empty", name)
				}
			}
		})
	}
}

// TestProfilePortsAreFrozen pins that only the HOST part of a service URL is
// environment-specific. A port that differs between profiles is a mistake: the
// port assignment is a platform-wide contract.
func TestProfilePortsAreFrozen(t *testing.T) {
	for _, profile := range allProfiles {
		t.Run(profile, func(t *testing.T) {
			cfg := loadProfile(t, profile)

			expected := map[string]struct {
				url  string
				port string
			}{
				"agent":    {cfg.AgentServiceURL(), ":8001"},
				"runtime":  {cfg.RuntimeServiceURL(), ":8002"},
				"identity": {cfg.IdentityServiceURL(), ":8003"},
				"control":  {cfg.ControlServiceURL(), ":8004"},
			}
			for name, want := range expected {
				if want.url == "" {
					continue // legitimately absent in the global tier
				}
				if !strings.HasSuffix(want.url, want.port) {
					t.Errorf("%s is published as %q, want it to end in %q", name, want.url, want.port)
				}
			}
		})
	}
}

// TestContainerProfilesAvoidLocalhost catches the single most common container
// configuration defect. Inside a container "localhost" resolves to the
// container, so the connection is refused or — worse — reaches a co-located
// process that is not the intended peer.
func TestContainerProfilesAvoidLocalhost(t *testing.T) {
	for _, profile := range containerProfiles {
		t.Run(profile, func(t *testing.T) {
			cfg := loadProfile(t, profile)

			candidates := map[string]string{
				"database.host": cfg.Database.Host,
				"agent url":     cfg.AgentServiceURL(),
				"runtime url":   cfg.RuntimeServiceURL(),
				"control url":   cfg.ControlServiceURL(),
				"identity url":  cfg.IdentityServiceURL(),
			}
			// Enabled is a *bool so a profile can turn Redis OFF explicitly;
			// nil means "inherited", which the base sets to true.
			if cfg.Redis.Enabled == nil || *cfg.Redis.Enabled {
				candidates["redis.host"] = cfg.Redis.Host
			}
			for what, value := range candidates {
				if value == "" {
					continue
				}
				if strings.Contains(value, "localhost") || strings.Contains(value, "127.0.0.1") {
					t.Errorf("%s = %q points at the container itself", what, value)
				}
			}
		})
	}
}

// TestGoAndPythonAgreeOnIdentitySplit pins the Global/Cell boundary as the Go
// loader sees it. identity-service resolves its OWN database through this
// override, so the two loaders disagreeing here would put the identity tables
// in different places depending on which service created them first.
func TestGoAndPythonAgreeOnIdentitySplit(t *testing.T) {
	for _, profile := range []string{"cell", "k8s-cell"} {
		t.Run(profile, func(t *testing.T) {
			cfg := loadProfile(t, profile)

			identity, ok := cfg.Database.Services["identity"]
			if !ok {
				t.Fatalf("%s ships no database.services.identity override", profile)
			}
			if identity.Host == cfg.Database.Host {
				t.Errorf("identity resolves to the CELL database (%s), defeating the Global/Cell split", identity.Host)
			}
		})
	}
}

// TestLoadPlatformControlURL pins the P5 topology contract: the
// services.control entry from platform.yaml must reach ControlServiceURL().
// It once didn't — mergePlatform skipped the Control entry, so every
// control-plane route dialed an empty URL and the deep health check
// reported `unsupported protocol scheme ""`.
func TestLoadPlatformControlURL(t *testing.T) {
	dir := t.TempDir()
	content := "services:\n  control:\n    rest_url: \"http://control-plane:9000\"\n"
	if err := os.WriteFile(filepath.Join(dir, "platform.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CONFIG_PATH", dir)
	t.Setenv("DATACODE_ENV", "")

	cfg := LoadPlatform()
	if got := cfg.ControlServiceURL(); got != "http://control-plane:9000" {
		t.Fatalf("ControlServiceURL() = %q, want %q", got, "http://control-plane:9000")
	}
}

// TestLoadPlatformWithoutConfigHasNoAddresses pins the deliberate absence of
// built-in addresses.
//
// This replaces an earlier test that asserted a default of
// "http://localhost:8004". That default was written to stop the topology from
// being silently blank — but it traded a visible failure for an invisible one:
// a pod whose ConfigMap failed to mount, or a process started without
// DATACODE_ENV, would proxy to whatever listens on the local port instead of
// reporting that it has no configuration. The empty-URL case is now caught
// where it can be acted on: `make doctor` in CI and before a release, plus the
// startup warning emitted by LoadPlatform.
func TestLoadPlatformWithoutConfigHasNoAddresses(t *testing.T) {
	t.Setenv("CONFIG_PATH", t.TempDir()) // empty dir: no platform.yaml found
	t.Setenv("DATACODE_ENV", "")

	cfg := LoadPlatform()

	for name, url := range map[string]string{
		"control":  cfg.ControlServiceURL(),
		"runtime":  cfg.RuntimeServiceURL(),
		"identity": cfg.IdentityServiceURL(),
		"agent":    cfg.AgentServiceURL(),
	} {
		if url != "" {
			t.Errorf("%s URL = %q with no config present, want empty: a built-in "+
				"address hides a missing config mount", name, url)
		}
	}

	// Non-address defaults must survive: they are workload properties, valid
	// everywhere, and their absence would only produce noise.
	if cfg.Redis.KeyPrefix != "datacode" {
		t.Errorf("redis.key_prefix = %q, want the built-in default", cfg.Redis.KeyPrefix)
	}
	if cfg.Logging.Level != "info" {
		t.Errorf("logging.level = %q, want the built-in default", cfg.Logging.Level)
	}
}
