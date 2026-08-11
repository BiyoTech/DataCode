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
func Load() *Config {
	cfg := &Config{
		Server:   ServerConfig{HTTPPort: 8000},
		Services: ServicesConfig{
			AgentServiceURL:    "http://localhost:8001",
			SQLMeshServiceURL:  "http://localhost:8002",
			OntologyServiceURL: "http://localhost:8003",
			DagsterServiceURL:  "http://localhost:8004",
		},
		CORS:    CORSConfig{FrontendOrigin: "http://localhost:5173"},
		Logging: LoggingConfig{Level: "info"},
	}

	// Load YAML config file
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

	return cfg
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
