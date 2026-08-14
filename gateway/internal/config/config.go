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
}

// AuthConfig holds authentication settings.
//
// The JWT signing secret is intentionally NOT part of this struct's YAML
// surface: secrets are env-only (GW_JWT_SECRET) and read separately via
// JWTSecret().
//
// The auth database connection is sourced from platform.yaml (database type,
// host, port, etc.) and exposed as AuthDialect + AuthDSN. The DBPath field
// is kept for backward compatibility with GW_AUTH_DB_PATH env override
// (SQLite-only).
type AuthConfig struct {
	// AuthDialect is the database backend: "sqlite" or "mysql".
	// Sourced from platform.yaml.
	AuthDialect string `yaml:"-"`
	// AuthDSN is the connection string passed to the auth store.
	// For SQLite: a file path. For MySQL: a go-sql-driver/mysql DSN.
	// Sourced from platform.yaml.
	AuthDSN string `yaml:"-"`
	// DBPath is kept for backward compatibility with GW_AUTH_DB_PATH.
	// When set, it overrides AuthDSN and forces SQLite dialect.
	DBPath string `yaml:"db_path"`
	// AccessTokenTTLMinutes controls the lifetime of the stateless access JWT.
	AccessTokenTTLMinutes int `yaml:"access_token_ttl_minutes"`
	// RefreshTokenTTLHours controls the lifetime of the opaque refresh token.
	RefreshTokenTTLHours int `yaml:"refresh_token_ttl_hours"`
	// PreAuthTokenTTLMinutes controls the lifetime of the short-lived token
	// issued when a user must pick one of several tenant identities.
	PreAuthTokenTTLMinutes int `yaml:"pre_auth_token_ttl_minutes"`
	// LoginMaxAttempts is the number of consecutive failures before lockout.
	LoginMaxAttempts int `yaml:"login_max_attempts"`
	// LoginLockoutMinutes is how long a locked login target stays locked.
	LoginLockoutMinutes int `yaml:"login_lockout_minutes"`
	// BcryptCost is the bcrypt work factor for password hashing.
	BcryptCost int `yaml:"bcrypt_cost"`
}

type ServerConfig struct {
	HTTPPort int `yaml:"http_port"`
}

type ServicesConfig struct {
	AgentServiceURL    string `yaml:"agent_service_url"`
	SQLMeshServiceURL  string `yaml:"sqlmesh_service_url"`
	OntologyServiceURL string `yaml:"ontology_service_url"`
	DagsterServiceURL  string `yaml:"dagster_service_url"`
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
		Server: ServerConfig{HTTPPort: 8000},
		Services: ServicesConfig{
			AgentServiceURL:    platform.AgentServiceURL(),
			SQLMeshServiceURL:  platform.SQLMeshServiceURL(),
			OntologyServiceURL: platform.OntologyServiceURL(),
			DagsterServiceURL:  platform.DagsterServiceURL(),
		},
		CORS:    CORSConfig{FrontendOrigin: "http://localhost:5173"},
		Logging: LoggingConfig{Level: platform.LogLevel()},
		Auth: AuthConfig{
			AuthDialect:            platform.AuthDBDialect(),
			AuthDSN:                platform.AuthDBDSN(".data/gateway.db"),
			AccessTokenTTLMinutes:  120,
			RefreshTokenTTLHours:   168,
			PreAuthTokenTTLMinutes: 5,
			LoginMaxAttempts:       5,
			LoginLockoutMinutes:    5,
			BcryptCost:             10,
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
	if v := os.Getenv("GW_SQLMESH_SERVICE_URL"); v != "" {
		cfg.Services.SQLMeshServiceURL = v
	}
	if v := os.Getenv("GW_FRONTEND_ORIGIN"); v != "" {
		cfg.CORS.FrontendOrigin = v
	}
	if v := os.Getenv("GW_ONTOLOGY_SERVICE_URL"); v != "" {
		cfg.Services.OntologyServiceURL = v
	}
	if v := os.Getenv("GW_DAGSTER_SERVICE_URL"); v != "" {
		cfg.Services.DagsterServiceURL = v
	}
	if v := os.Getenv("GW_AUTH_DB_PATH"); v != "" {
		// GW_AUTH_DB_PATH forces SQLite dialect (backward compatibility).
		cfg.Auth.AuthDialect = "sqlite"
		cfg.Auth.AuthDSN = resolveDataPath(v, configPath)
		cfg.Auth.DBPath = cfg.Auth.AuthDSN
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
