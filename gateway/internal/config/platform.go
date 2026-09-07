// Package config provides configuration for the API Gateway.
//
// platform.go loads the shared platform.yaml (+ optional platform.{env}.yaml
// override) that is consumed by all services. The Gateway reads it for
// cross-cutting concerns: service topology, logging, and workspace paths.
package config

import (
	"fmt"
	"log"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
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
// services. The Gateway reads it, optionally narrowed by the
// `services.gateway_auth` entry, to locate its auth database.
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
	Gateway  PlatformServiceEntry `yaml:"gateway"`
	Agent    PlatformServiceEntry `yaml:"agent"`
	Runtime  PlatformServiceEntry `yaml:"runtime"`
	Identity PlatformServiceEntry `yaml:"identity"`
	Control  PlatformServiceEntry `yaml:"control"`
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
// Addresses have NO code defaults on purpose. A built-in "http://localhost:8004"
// makes a deployment that failed to mount its config, or forgot to select a
// profile, dial an arbitrary local port instead of failing — and a gateway that
// proxies to the wrong process is far harder to diagnose than one that reports
// an empty upstream. The Python loader behaves identically, so the two
// implementations cannot disagree about what "unconfigured" means.
//
// Non-address defaults (pool sizes, timeouts, key prefix, log level) ARE set
// here: they are properties of the workload, valid in every environment, and
// their absence would only produce noise.
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
	// deliberately address-free, so without a profile every URL below stays at
	// its code default — not wrong so much as arbitrary, and dialing an
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
	mergeServiceEntry(&base.Services.Identity, override.Services.Identity)
	mergeServiceEntry(&base.Services.Control, override.Services.Control)

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

// AuthDBDialect and the auth-database resolution chain were removed in P5.5
// (see the note further down): the gateway opens no database.

// authDBParams and its resolution were removed with the auth database.

// P5.5 NOTE — the gateway opens NO database.
//
// This file used to resolve an "auth database" connection (dialect, DSN,
// per-service overrides, MySQL/Postgres DSN builders). identity-service owns
// the identity tables since P2 and is their only writer since P5.5, so the
// gateway holds no connection at all and that whole resolution chain was
// removed. `database.services.gateway_auth` in platform.yaml is likewise
// obsolete: identity-service reads its own entry.

// AgentServiceURL returns the agent service URL from platform topology.
func (p *PlatformConfig) AgentServiceURL() string {
	if u := p.Services.Agent.RestURL; u != "" {
		return u
	}
	return p.Services.Agent.WSURL
}

// RuntimeServiceURL returns the workspace-runtime URL from platform topology
// (P3). Empty until the deployment adds services.runtime to platform.yaml —
// the gateway's working-copy routes then have no upstream, which surfaces
// immediately as proxy errors rather than silently routing somewhere else.
func (p *PlatformConfig) RuntimeServiceURL() string {
	return p.Services.Runtime.RestURL
}

// ControlServiceURL returns the control-service URL from platform topology
// (P5). Empty until the deployment adds services.control to platform.yaml —
// the metadata routes then have no upstream, which surfaces immediately as
// proxy errors rather than silently routing somewhere else.
func (p *PlatformConfig) ControlServiceURL() string {
	return p.Services.Control.RestURL
}

// IdentityServiceURL returns the identity service REST URL from platform
// topology (P2). The gateway proxies /auth, /me and /rbac there and resolves
// authorization snapshots from it.
func (p *PlatformConfig) IdentityServiceURL() string {
	return p.Services.Identity.RestURL
}

// LogLevel returns the shared log level.
func (p *PlatformConfig) LogLevel() string {
	return p.Logging.Level
}

// Ensure the import of filepath is used (resolveDataPath and resolveConfigPath
// are defined in config.go, but we reference filepath here to avoid unused
// import if this file is compiled standalone during testing).
var _ = filepath.Join
