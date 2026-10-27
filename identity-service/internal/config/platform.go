// Package config provides configuration for the API Gateway.
//
// platform.go loads the shared platform.yaml (+ optional platform.{env}.yaml
// override) that is consumed by all services. The Gateway reads it for
// cross-cutting concerns: service topology, logging, and workspace paths.
package config

import (
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"

	"github.com/datacode/identity-service/internal/dbx"
)

// Default ports per dialect, applied when the config omits one.
const (
	defaultMySQLPort    = 3306
	defaultPostgresPort = 5432
)

// PlatformConfig holds shared cross-service configuration loaded from
// conf/platform.yaml. It is read alongside the gateway-specific config.
type PlatformConfig struct {
	Database  PlatformDatabase  `yaml:"database"`
	Redis     PlatformRedis     `yaml:"redis"`
	Workspace PlatformWorkspace `yaml:"workspace"`
	Services  PlatformServices  `yaml:"services"`
	Logging   PlatformLogging   `yaml:"logging"`
}

// PlatformRedis holds the shared coordination backend used for state that must
// be identical across gateway replicas (currently login rate limiting).
//
// Enabled is a pointer so an override file can turn Redis OFF: mergePlatform
// only copies non-zero values, and a plain `false` bool is indistinguishable
// from "not set in this file".
//
// The password is env-only (REDIS_PASSWORD) and never read from YAML.
type PlatformRedis struct {
	Enabled                 *bool   `yaml:"enabled"`
	Host                    string  `yaml:"host"`
	Port                    int     `yaml:"port"`
	DB                      int     `yaml:"db"`
	KeyPrefix               string  `yaml:"key_prefix"`
	ConnectTimeoutSeconds   float64 `yaml:"connect_timeout_seconds"`
	OperationTimeoutSeconds float64 `yaml:"operation_timeout_seconds"`
	MaxConnections          int     `yaml:"max_connections"`
}

// PlatformDatabase holds the default database connection shared across
// services. identity-service reads it, optionally narrowed by the
// `services.identity` entry, to locate the identity database it owns.
type PlatformDatabase struct {
	Type     string `yaml:"type"`
	Host     string `yaml:"host"`
	Port     int    `yaml:"port"`
	Name     string `yaml:"name"`
	Username string `yaml:"username"`
	// SSLMode applies to PostgreSQL only. Left empty, the driver's own default
	// is used rather than a hardcoded downgrade.
	SSLMode  string                          `yaml:"ssl_mode"`
	Services map[string]PlatformDatabaseSpec `yaml:"services"`
}

// PlatformDatabaseSpec is a per-service database override.
type PlatformDatabaseSpec struct {
	Type       string `yaml:"type"`
	SQLitePath string `yaml:"sqlite_path"`
	Host       string `yaml:"host"`
	Port       int    `yaml:"port"`
	Name       string `yaml:"name"`
	Username   string `yaml:"username"`
	SSLMode    string `yaml:"ssl_mode"`
}

// PlatformWorkspace holds the shared workspace storage path.
type PlatformWorkspace struct {
	BasePath string `yaml:"base_path"`
}

// PlatformServices is the inter-service endpoint topology. The retired
// sqlmesh/ontology/dagster entries (standalone services merged into
// workspace-runtime at P3) are gone: nothing registers them and nothing
// consumes them.
type PlatformServices struct {
	Gateway PlatformServiceEntry `yaml:"gateway"`
	Agent   PlatformServiceEntry `yaml:"agent"`
	Runtime PlatformServiceEntry `yaml:"runtime"`
}

// PlatformServiceEntry holds endpoint URLs for a single service.
type PlatformServiceEntry struct {
	HTTPURL    string `yaml:"http_url"`
	WSURL      string `yaml:"ws_url"`
	GRPCURL    string `yaml:"grpc_url"`
	RestURL    string `yaml:"rest_url"`
	GraphqlURL string `yaml:"graphql_url"`
}

// PlatformLogging holds shared log settings.
type PlatformLogging struct {
	Level  string `yaml:"level"`
	Format string `yaml:"format"`
}

// LoadPlatform reads conf/platform.yaml + the profile override selected by
// DATACODE_ENV, then merges them (the profile wins).
//
// Addresses have NO code defaults on purpose: a built-in localhost makes a
// deployment that failed to mount its config connect to an arbitrary local
// port instead of failing. The Python loader and the gateway's copy behave
// identically, so no two implementations can disagree about what
// "unconfigured" means.
func LoadPlatform() *PlatformConfig {
	cfg := &PlatformConfig{
		Database: PlatformDatabase{
			Services: map[string]PlatformDatabaseSpec{},
		},
		Logging: PlatformLogging{Level: "info", Format: "json"},
		Redis: PlatformRedis{
			Port:                    6379,
			KeyPrefix:               "datacode",
			ConnectTimeoutSeconds:   3,
			OperationTimeoutSeconds: 3,
			MaxConnections:          32,
		},
	}

	// Load base platform.yaml.
	if path := resolveConfigPath("platform.yaml"); path != "" {
		applyYAML(path, cfg)
	}

	// Load the profile override selected by DATACODE_ENV. conf/platform.yaml is
	// deliberately address-free, so without a profile every address stays at its
	// code default — not wrong so much as arbitrary, and connecting to an
	// arbitrary localhost is precisely what these warnings exist to surface.
	// `make doctor` treats the same conditions as errors.
	if env := os.Getenv("DATACODE_ENV"); env != "" {
		if path := resolveConfigPath(fmt.Sprintf("platform.%s.yaml", env)); path != "" {
			applyYAML(path, cfg)
		} else {
			// A typo here is otherwise indistinguishable from "no profile".
			log.Printf("config_profile_not_found profile=%s expected_file=platform.%s.yaml"+
				" -- running on base defaults, which carry no usable addresses", env, env)
		}
	} else {
		log.Printf("config_profile_unset var=DATACODE_ENV" +
			" -- expected one of dev|docker|cell|global|k8s-cell|k8s-global;" +
			" platform addresses will fall back to code defaults")
	}

	return cfg
}

// applyYAML reads a YAML file and merges it into cfg via re-decoding.
// This is a shallow merge: top-level keys from the file replace cfg fields.
// For nested maps (e.g. database.services) the override replaces the whole sub-map.
func applyYAML(path string, cfg *PlatformConfig) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	// Decode into a temp struct, then manually merge so we don't lose
	// default values for keys absent in the override file.
	var tmp PlatformConfig
	if err := yaml.Unmarshal(data, &tmp); err != nil {
		return
	}
	mergePlatform(cfg, &tmp)
}

// mergePlatform merges override into base (override wins for non-zero values).
func mergePlatform(base, override *PlatformConfig) {
	// Database
	if override.Database.Type != "" {
		base.Database.Type = override.Database.Type
	}
	if override.Database.Host != "" {
		base.Database.Host = override.Database.Host
	}
	if override.Database.Port != 0 {
		base.Database.Port = override.Database.Port
	}
	if override.Database.Name != "" {
		base.Database.Name = override.Database.Name
	}
	if override.Database.Username != "" {
		base.Database.Username = override.Database.Username
	}
	if override.Database.SSLMode != "" {
		base.Database.SSLMode = override.Database.SSLMode
	}
	for k, v := range override.Database.Services {
		base.Database.Services[k] = v
	}

	// Workspace
	if override.Workspace.BasePath != "" {
		base.Workspace.BasePath = override.Workspace.BasePath
	}

	// Redis
	mergeRedis(&base.Redis, override.Redis)

	// Services — only override non-empty fields.
	mergeServiceEntry(&base.Services.Gateway, override.Services.Gateway)
	mergeServiceEntry(&base.Services.Agent, override.Services.Agent)
	mergeServiceEntry(&base.Services.Runtime, override.Services.Runtime)

	// Logging
	if override.Logging.Level != "" {
		base.Logging.Level = override.Logging.Level
	}
	if override.Logging.Format != "" {
		base.Logging.Format = override.Logging.Format
	}
}

// mergeRedis copies non-zero fields from override into base.
// Enabled is a pointer precisely so `enabled: false` survives this merge.
func mergeRedis(base *PlatformRedis, override PlatformRedis) {
	if override.Enabled != nil {
		base.Enabled = override.Enabled
	}
	if override.Host != "" {
		base.Host = override.Host
	}
	if override.Port != 0 {
		base.Port = override.Port
	}
	if override.DB != 0 {
		base.DB = override.DB
	}
	if override.KeyPrefix != "" {
		base.KeyPrefix = override.KeyPrefix
	}
	if override.ConnectTimeoutSeconds != 0 {
		base.ConnectTimeoutSeconds = override.ConnectTimeoutSeconds
	}
	if override.OperationTimeoutSeconds != 0 {
		base.OperationTimeoutSeconds = override.OperationTimeoutSeconds
	}
	if override.MaxConnections != 0 {
		base.MaxConnections = override.MaxConnections
	}
}

// RedisEnabled reports whether the shared coordination backend should be used.
// Absent configuration means enabled: Redis is a platform dependency, and a
// gateway that silently kept per-replica state would widen the brute-force
// window it is supposed to close.
func (p *PlatformConfig) RedisEnabled() bool {
	return p.Redis.Enabled == nil || *p.Redis.Enabled
}

// mergeServiceEntry copies non-empty fields from override into base.
func mergeServiceEntry(base *PlatformServiceEntry, override PlatformServiceEntry) {
	if override.HTTPURL != "" {
		base.HTTPURL = override.HTTPURL
	}
	if override.WSURL != "" {
		base.WSURL = override.WSURL
	}
	if override.GRPCURL != "" {
		base.GRPCURL = override.GRPCURL
	}
	if override.RestURL != "" {
		base.RestURL = override.RestURL
	}
	if override.GraphqlURL != "" {
		base.GraphqlURL = override.GraphqlURL
	}
}

// AuthDBDialect returns the database dialect for the identity store.
//
// Resolution order:
//  1. If database.services.identity exists, use its type.
//  2. Otherwise, use the platform base database type.
//
// Returns "sqlite", "mysql" or "postgres". An unrecognized type falls back to
// SQLite, which is the only backend that cannot fail for lack of a server.
func (p *PlatformConfig) AuthDBDialect() string {
	raw := p.Database.Type
	if svc, ok := p.Database.Services["identity"]; ok && svc.Type != "" {
		raw = svc.Type
	}
	dialect, ok := dbx.Normalize(raw)
	if !ok {
		return string(dbx.SQLite)
	}
	return string(dialect)
}

// authDBParams resolves the effective connection parameters for the auth
// store: the `identity` override wins field by field over the platform base
// database, so a deployment can move just the identity store elsewhere without
// restating the whole connection.
type authDBParams struct {
	host     string
	port     int
	name     string
	username string
	sslMode  string
}

func (p *PlatformConfig) authDBParams() authDBParams {
	params := authDBParams{
		host:     p.Database.Host,
		port:     p.Database.Port,
		name:     p.Database.Name,
		username: p.Database.Username,
		sslMode:  p.Database.SSLMode,
	}
	svc, ok := p.Database.Services["identity"]
	if !ok {
		return params
	}
	if svc.Host != "" {
		params.host = svc.Host
	}
	if svc.Port != 0 {
		params.port = svc.Port
	}
	if svc.Name != "" {
		params.name = svc.Name
	}
	if svc.Username != "" {
		params.username = svc.Username
	}
	if svc.SSLMode != "" {
		params.sslMode = svc.SSLMode
	}
	return params
}

// AuthDBDSN builds the DSN for the identity database connection.
//
// The shape is dialect-specific:
//   - SQLite:     a file path resolved relative to the repo root, taken from
//     database.services.identity.sqlite_path or defaultSQLitePath.
//   - MySQL:      a go-sql-driver DSN.
//   - PostgreSQL: a pgx/libpq URL.
//
// The password always comes from the DB_PASSWORD environment variable and is
// never read from YAML.
func (p *PlatformConfig) AuthDBDSN(defaultSQLitePath string) string {
	switch dbx.Dialect(p.AuthDBDialect()) {
	case dbx.MySQL:
		return p.buildMySQLDSN()
	case dbx.Postgres:
		return p.buildPostgresDSN()
	}

	// SQLite: resolve file path.
	svc, ok := p.Database.Services["identity"]
	if ok && svc.SQLitePath != "" {
		return resolveDataPath(svc.SQLitePath, resolveConfigPath("platform.yaml"))
	}
	return resolveDataPath(defaultSQLitePath, resolveConfigPath("platform.yaml"))
}

// buildMySQLDSN constructs a go-sql-driver/mysql DSN from platform config.
func (p *PlatformConfig) buildMySQLDSN() string {
	params := p.authDBParams()
	if params.port == 0 {
		params.port = defaultMySQLPort
	}
	// parseTime=true is required for the driver to scan DATETIME columns into
	// time.Time. multiStatements is NOT enabled: it would turn any injection
	// defect into arbitrary statement execution.
	return fmt.Sprintf("%s:%s@tcp(%s:%d)/%s?parseTime=true&charset=utf8mb4",
		params.username, os.Getenv("DB_PASSWORD"), params.host, params.port, params.name)
}

// buildPostgresDSN constructs a pgx/libpq URL from platform config.
//
// Credentials are percent-encoded because they travel inside a URL: an
// unescaped '@' or '/' in a password would silently truncate the DSN and point
// the gateway at the wrong host.
//
// sslmode is only emitted when configured, leaving the driver's own default in
// place otherwise — hardcoding "disable" here would quietly downgrade every
// deployment that did not think to override it.
func (p *PlatformConfig) buildPostgresDSN() string {
	params := p.authDBParams()
	if params.port == 0 {
		params.port = defaultPostgresPort
	}

	dsn := fmt.Sprintf("postgres://%s:%s@%s:%d/%s",
		url.QueryEscape(params.username),
		url.QueryEscape(os.Getenv("DB_PASSWORD")),
		params.host, params.port, params.name)
	if params.sslMode != "" {
		dsn += "?sslmode=" + url.QueryEscape(params.sslMode)
	}
	return dsn
}

// The per-service URL accessors the gateway needs (agent / runtime / ...) are
// deliberately absent: identity-service dials no other service, so an accessor
// here would only be a way to grow one unnoticed.

// LogLevel returns the shared log level.
func (p *PlatformConfig) LogLevel() string {
	return p.Logging.Level
}

// Ensure the import of filepath is used (resolveDataPath and resolveConfigPath
// are defined in config.go, but we reference filepath here to avoid unused
// import if this file is compiled standalone during testing).
var _ = filepath.Join
