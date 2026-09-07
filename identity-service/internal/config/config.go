// Package config provides configuration for the API Gateway.
// Loads from YAML file (conf/gateway.yaml) with environment variable overrides.
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Config holds identity-service configuration.
//
// It carries NO service-topology section: identity-service proxies nothing and
// dials no other service, so the inter-service URLs the gateway needs would be
// dead configuration here.
type Config struct {
	Server  ServerConfig  `yaml:"server"`
	CORS    CORSConfig    `yaml:"cors"`
	Logging LoggingConfig `yaml:"logging"`
	Auth    AuthConfig    `yaml:"auth"`
	// Platform is the resolved shared configuration. It is retained (rather
	// than only projected into the fields above) because cross-cutting
	// dependencies such as the Redis coordination backend are wired from it at
	// startup, and re-loading the file would risk a different answer.
	Platform *PlatformConfig `yaml:"-"`
}

// AuthConfig holds authentication settings.
//
// The JWT signing secret is intentionally NOT part of this struct's YAML
// surface: secrets are env-only (GW_JWT_SECRET) and read separately via
// JWTSecret(). The name is shared with the gateway on purpose: identity mints
// the tokens and the gateway verifies them, so one secret serves both.
//
// The identity database connection is sourced from platform.yaml (database
// type, host, port, optionally narrowed by `database.services.identity`) and
// exposed as AuthDialect + AuthDSN.
type AuthConfig struct {
	// AuthDialect is the database backend: "sqlite", "mysql" or "postgres".
	// Sourced from platform.yaml.
	AuthDialect string `yaml:"-"`
	// AuthDSN is the connection string passed to the identity store: a file
	// path for SQLite, a go-sql-driver DSN for MySQL, a pgx URL for PostgreSQL.
	// Sourced from platform.yaml.
	AuthDSN string `yaml:"-"`
	// DBPath overrides AuthDSN with a SQLite file, for zero-dependency dev
	// runs. Set through IDENTITY_SERVICE_DB_PATH.
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
		CORS:     CORSConfig{FrontendOrigin: "http://localhost:5173"},
		Logging:  LoggingConfig{Level: platform.LogLevel()},
		Auth: AuthConfig{
			AuthDialect:            platform.AuthDBDialect(),
			AuthDSN:                platform.AuthDBDSN(".data/identity.db"),
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
	if v := os.Getenv("GW_FRONTEND_ORIGIN"); v != "" {
		cfg.CORS.FrontendOrigin = v
	}
	if v := os.Getenv("IDENTITY_SERVICE_DB_PATH"); v != "" {
		// A SQLite file for zero-dependency dev runs; forces the dialect.
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
