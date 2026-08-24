// Package config provides configuration for the API Gateway.
//
// platform.go loads the shared platform.yaml (+ optional platform.{env}.yaml
// override) that is consumed by all services. The Gateway reads it for
// cross-cutting concerns: service topology, logging, and workspace paths.
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// PlatformConfig holds shared cross-service configuration loaded from
// conf/platform.yaml. It is read alongside the gateway-specific config.
type PlatformConfig struct {
	Database  PlatformDatabase  `yaml:"database"`
	Workspace PlatformWorkspace `yaml:"workspace"`
	Services  PlatformServices  `yaml:"services"`
	Logging   PlatformLogging   `yaml:"logging"`
}

// PlatformDatabase holds the default database connection shared across
// services. The Gateway only uses the `services.gateway_auth` entry to
// locate its auth SQLite database.
type PlatformDatabase struct {
	Type     string                          `yaml:"type"`
	Host     string                          `yaml:"host"`
	Port     int                             `yaml:"port"`
	Name     string                          `yaml:"name"`
	Username string                          `yaml:"username"`
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
}

// PlatformWorkspace holds the shared workspace storage path.
type PlatformWorkspace struct {
	BasePath string `yaml:"base_path"`
}

// PlatformServices is the inter-service endpoint topology.
type PlatformServices struct {
	Gateway  PlatformServiceEntry `yaml:"gateway"`
	Agent    PlatformServiceEntry `yaml:"agent"`
	SQLMesh  PlatformServiceEntry `yaml:"sqlmesh"`
	Ontology PlatformServiceEntry `yaml:"ontology"`
	Dagster  PlatformServiceEntry `yaml:"dagster"`
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

// LoadPlatform reads conf/platform.yaml + optional platform.{DATACODE_ENV}.yaml
// override, then merges them (env override wins). Returns a zero-value
// PlatformConfig when no file is found.
func LoadPlatform() *PlatformConfig {
	cfg := &PlatformConfig{
		Database: PlatformDatabase{
			Services: map[string]PlatformDatabaseSpec{},
		},
		Services: PlatformServices{
			Agent:    PlatformServiceEntry{WSURL: "ws://localhost:8001", GRPCURL: "localhost:50051", RestURL: "http://localhost:8001"},
			SQLMesh:  PlatformServiceEntry{RestURL: "http://localhost:8002", GRPCURL: "localhost:50052"},
			Ontology: PlatformServiceEntry{RestURL: "http://localhost:8003"},
			Dagster:  PlatformServiceEntry{RestURL: "http://localhost:8004", GraphqlURL: "http://localhost:3001/graphql"},
			Gateway:  PlatformServiceEntry{HTTPURL: "http://localhost:8000"},
		},
		Logging: PlatformLogging{Level: "info", Format: "json"},
	}

	// Load base platform.yaml.
	if path := resolveConfigPath("platform.yaml"); path != "" {
		applyYAML(path, cfg)
	}

	// Load optional env override.
	if env := os.Getenv("DATACODE_ENV"); env != "" {
		if path := resolveConfigPath(fmt.Sprintf("platform.%s.yaml", env)); path != "" {
			applyYAML(path, cfg)
		}
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
	for k, v := range override.Database.Services {
		base.Database.Services[k] = v
	}

	// Workspace
	if override.Workspace.BasePath != "" {
		base.Workspace.BasePath = override.Workspace.BasePath
	}

	// Services — only override non-empty fields.
	mergeServiceEntry(&base.Services.Gateway, override.Services.Gateway)
	mergeServiceEntry(&base.Services.Agent, override.Services.Agent)
	mergeServiceEntry(&base.Services.SQLMesh, override.Services.SQLMesh)
	mergeServiceEntry(&base.Services.Ontology, override.Services.Ontology)
	mergeServiceEntry(&base.Services.Dagster, override.Services.Dagster)

	// Logging
	if override.Logging.Level != "" {
		base.Logging.Level = override.Logging.Level
	}
	if override.Logging.Format != "" {
		base.Logging.Format = override.Logging.Format
	}
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

// AuthDBDialect returns the database dialect for the gateway auth store.
//
// Resolution order:
//  1. If database.services.gateway_auth exists, use its type.
//  2. Otherwise, use the platform base database type.
//
// Returns "sqlite" or "mysql". Unknown types default to "sqlite".
func (p *PlatformConfig) AuthDBDialect() string {
	if svc, ok := p.Database.Services["gateway_auth"]; ok && svc.Type != "" {
		return svc.Type
	}
	if p.Database.Type != "" {
		return p.Database.Type
	}
	return "sqlite"
}

// AuthDBDSN builds the DSN for the gateway auth database connection.
//
// For SQLite: returns a file path (resolved relative to the repo root).
//   - Uses database.services.gateway_auth.sqlite_path when present.
//   - Falls back to defaultSQLitePath.
//
// For MySQL: builds a go-sql-driver/mysql DSN string.
//   - Uses database.services.gateway_auth for host/port/name/username when present.
//   - Falls back to the platform base database connection params.
//   - Password is read from the DB_PASSWORD env var (never stored in YAML).
//
// The defaultSQLitePath is used only when the dialect resolves to SQLite and
// no gateway_auth override is configured.
func (p *PlatformConfig) AuthDBDSN(defaultSQLitePath string) string {
	dialect := p.AuthDBDialect()

	if dialect == "mysql" {
		return p.buildMySQLDSN()
	}

	// SQLite: resolve file path.
	svc, ok := p.Database.Services["gateway_auth"]
	if ok && svc.SQLitePath != "" {
		return resolveDataPath(svc.SQLitePath, resolveConfigPath("platform.yaml"))
	}
	return resolveDataPath(defaultSQLitePath, resolveConfigPath("platform.yaml"))
}

// buildMySQLDSN constructs a go-sql-driver/mysql DSN from platform config.
// Password comes from the DB_PASSWORD environment variable.
func (p *PlatformConfig) buildMySQLDSN() string {
	// Use gateway_auth override if present, else fall back to base config.
	host := p.Database.Host
	port := p.Database.Port
	name := p.Database.Name
	user := p.Database.Username

	if svc, ok := p.Database.Services["gateway_auth"]; ok {
		if svc.Host != "" {
			host = svc.Host
		}
		if svc.Port != 0 {
			port = svc.Port
		}
		if svc.Name != "" {
			name = svc.Name
		}
		if svc.Username != "" {
			user = svc.Username
		}
	}

	password := os.Getenv("DB_PASSWORD")
	if port == 0 {
		port = 3306
	}
	// parseTime=true is required for the driver to scan DATETIME columns into time.Time.
	// multiStatements=true is NOT enabled for security.
	return fmt.Sprintf("%s:%s@tcp(%s:%d)/%s?parseTime=true&charset=utf8mb4",
		user, password, host, port, name)
}

// AgentServiceURL returns the agent service URL from platform topology.
func (p *PlatformConfig) AgentServiceURL() string {
	if u := p.Services.Agent.RestURL; u != "" {
		return u
	}
	return p.Services.Agent.WSURL
}

// SQLMeshServiceURL returns the sqlmesh service REST URL from platform topology.
func (p *PlatformConfig) SQLMeshServiceURL() string {
	return p.Services.SQLMesh.RestURL
}

// OntologyServiceURL returns the ontology service REST URL from platform topology.
func (p *PlatformConfig) OntologyServiceURL() string {
	return p.Services.Ontology.RestURL
}

// DagsterServiceURL returns the dagster service REST URL from platform topology.
func (p *PlatformConfig) DagsterServiceURL() string {
	return p.Services.Dagster.RestURL
}

// LogLevel returns the shared log level.
func (p *PlatformConfig) LogLevel() string {
	return p.Logging.Level
}

// Ensure the import of filepath is used (resolveDataPath and resolveConfigPath
// are defined in config.go, but we reference filepath here to avoid unused
// import if this file is compiled standalone during testing).
var _ = filepath.Join
