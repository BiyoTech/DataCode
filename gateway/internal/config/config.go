// Package config provides configuration for the API Gateway.
// Loads from YAML file (conf/gateway.yaml) with environment variable overrides.
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Config holds API Gateway configuration.
type Config struct {
	Server   ServerConfig   `yaml:"server"`
	Services ServicesConfig `yaml:"services"`
	CORS     CORSConfig     `yaml:"cors"`
	Logging  LoggingConfig  `yaml:"logging"`
	Auth     AuthConfig     `yaml:"auth"`
	// Platform is the resolved shared configuration. It is retained (rather
	// than only projected into the fields above) because cross-cutting
	// dependencies such as the Redis coordination backend are wired from it at
	// startup, and re-loading the file would risk a different answer.
	Platform *PlatformConfig `yaml:"-"`
}

// AuthConfig holds token verification settings.
//
// The JWT signing secret is intentionally NOT part of this struct's YAML
// surface: secrets are env-only (GW_JWT_SECRET) and read separately via
// JWTSecret().
//
// P5.5: the gateway holds NO database connection any more. identity-service
// owns the identity tables, so the former auth-database fields (dialect, DSN,
// db_path) and the password/lockout policy (bcrypt cost, login attempt limits)
// were removed from here — they are configured in identity-service, and a
// second copy could only drift.
//
// The token TTLs remain because the gateway must know how long a token it
// VERIFIES may live, and identity-service reads the same platform defaults.
type AuthConfig struct {
	// AccessTokenTTLMinutes controls the lifetime of the stateless access JWT.
	AccessTokenTTLMinutes int `yaml:"access_token_ttl_minutes"`
	// RefreshTokenTTLHours controls the lifetime of the opaque refresh token.
	RefreshTokenTTLHours int `yaml:"refresh_token_ttl_hours"`
	// PreAuthTokenTTLMinutes controls the lifetime of the short-lived token
	// issued when a user must pick one of several tenant identities.
	PreAuthTokenTTLMinutes int `yaml:"pre_auth_token_ttl_minutes"`
	// AuthzCacheTTLSeconds bounds how long an authorization snapshot from
	// identity-service is reused. Staleness is additionally cut short by the
	// perms_version claim, so this only bounds version-independent changes.
	AuthzCacheTTLSeconds int `yaml:"authz_cache_ttl_seconds"`
}

type ServerConfig struct {
	HTTPPort int `yaml:"http_port"`
}

type ServicesConfig struct {
	AgentServiceURL string `yaml:"agent_service_url"`
	// RuntimeServiceURL is workspace-runtime (P3): the merged owner of
	// working copies (sqlmesh engine + ontology + dagster data plane + git
	// + files + tools). Every working-copy-facing route group targets it.
	RuntimeServiceURL string `yaml:"runtime_service_url"`
	// ControlServiceURL is control-service (P5): the control plane owning
	// the environment registry, workspace metadata and Git credentials.
	// Metadata-facing route groups use it.
	ControlServiceURL string `yaml:"control_service_url"`
	// IdentityServiceURL is identity-service (P2): the global source of truth
	// for identity, authorization and tenant placement. The gateway proxies
	// /auth, /me and /rbac there and resolves authorization snapshots from it.
	IdentityServiceURL string `yaml:"identity_service_url"`
}

type CORSConfig struct {
	FrontendOrigin string `yaml:"frontend_origin"`
}

type LoggingConfig struct {
	Level string `yaml:"level"`
}

// Load reads configuration from YAML file, then applies env var overrides.
// YAML path resolution: env CONFIG_PATH > ../conf/gateway.yaml > ./conf/gateway.yaml
//
// Shared cross-cutting config (service topology, logging, auth DB path) is
// sourced from conf/platform.yaml via LoadPlatform(). Gateway-specific YAML
// overrides still take precedence for service URLs and logging.
func Load() *Config {
	// Load shared platform config first (provides defaults).
	platform := LoadPlatform()

	cfg := &Config{
		Platform: platform,
		Server:   ServerConfig{HTTPPort: 8000},
		Services: ServicesConfig{
			AgentServiceURL:    platform.AgentServiceURL(),
			RuntimeServiceURL:  platform.RuntimeServiceURL(),
			ControlServiceURL:  platform.ControlServiceURL(),
			IdentityServiceURL: platform.IdentityServiceURL(),
		},
		CORS:    CORSConfig{FrontendOrigin: "http://localhost:5173"},
		Logging: LoggingConfig{Level: platform.LogLevel()},
		Auth: AuthConfig{
			AccessTokenTTLMinutes:  120,
			RefreshTokenTTLHours:   168,
			PreAuthTokenTTLMinutes: 5,
			AuthzCacheTTLSeconds:   60,
		},
	}

	// Load gateway-specific YAML — overrides platform defaults for
	// service URLs, logging, and auth settings.
	configPath := resolveConfigPath("gateway.yaml")
	if configPath != "" {
		data, err := os.ReadFile(configPath)
		if err == nil {
			yaml.Unmarshal(data, cfg)
		}
	}

	// Environment variable overrides (highest priority)
	if v := os.Getenv("GW_HTTP_PORT"); v != "" {
		fmt.Sscanf(v, "%d", &cfg.Server.HTTPPort)
	}
	if v := os.Getenv("GW_AGENT_SERVICE_URL"); v != "" {
		cfg.Services.AgentServiceURL = v
	}
	if v := os.Getenv("GW_FRONTEND_ORIGIN"); v != "" {
		cfg.CORS.FrontendOrigin = v
	}
	if v := os.Getenv("GW_IDENTITY_SERVICE_URL"); v != "" {
		cfg.Services.IdentityServiceURL = v
	}
	if v := os.Getenv("GW_ACCESS_TOKEN_TTL_MINUTES"); v != "" {
		fmt.Sscanf(v, "%d", &cfg.Auth.AccessTokenTTLMinutes)
	}
	if v := os.Getenv("GW_REFRESH_TOKEN_TTL_HOURS"); v != "" {
		fmt.Sscanf(v, "%d", &cfg.Auth.RefreshTokenTTLHours)
	}

	return cfg
}

// JWTSecret returns the JWT signing secret. Secrets are env-only and never
// read from configuration files.
func JWTSecret() string {
	return os.Getenv("GW_JWT_SECRET")
}

// resolveDataPath makes a relative data path absolute. The repository root is
// derived from the resolved config file location (<root>/conf/<file>.yaml);
// when no config file was found it falls back to the working directory.
func resolveDataPath(path, configPath string) string {
	if path == "" || filepath.IsAbs(path) {
		return path
	}
	if configPath != "" {
		if abs, err := filepath.Abs(filepath.Join(filepath.Dir(configPath), "..", path)); err == nil {
			return abs
		}
	}
	if abs, err := filepath.Abs(path); err == nil {
		return abs
	}
	return path
}

// resolveConfigPath finds the YAML config file.
// Priority: CONFIG_PATH env > ../../conf/ > ../conf/ > ./conf/
func resolveConfigPath(filename string) string {
	if v := os.Getenv("CONFIG_PATH"); v != "" {
		p := filepath.Join(v, filename)
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	candidates := []string{
		filepath.Join("..", "..", "conf", filename),
		filepath.Join("..", "conf", filename),
		filepath.Join("conf", filename),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}
