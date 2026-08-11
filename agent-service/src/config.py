"""Agent Service configuration.

Loads from YAML file (conf/agent-service.yaml) with environment variable overrides.
Priority: env vars > YAML file > defaults.
"""

import os
from pathlib import Path
from typing import List, Optional

from agenticdata_common.config.yaml_loader import flatten_yaml_mapping, load_yaml_config
from agenticdata_common.database.url import build_database_url
from pydantic import BaseModel
from pydantic_settings import BaseSettings

_CONFIG_FILE = Path(__file__)


def _load_yaml_config(filename: str) -> dict:
    return load_yaml_config(filename, package_file=_CONFIG_FILE)


class LLMModelConfig(BaseModel):
    """Configuration for a single LLM model (preset or user-defined)."""
    id: str                          # Unique model identifier (e.g. "gpt-4o")
    name: str                        # Display name shown in frontend
    provider: str = "openai"         # Provider: "openai", "anthropic", "custom"
    api_key: str = ""                # API key; empty falls back to provider default
    base_url: str = ""               # Custom API endpoint (Azure, self-hosted, etc.)
    max_tokens: int = 8192           # Max output tokens


class LLMConfig(BaseModel):
    """Multi-model LLM configuration.
    Users can select from preset models or add custom models via frontend.
    """
    default_model: str = "gpt-4o"    # Default model ID when user doesn't specify
    models: List[LLMModelConfig] = [
        LLMModelConfig(id="gpt-4o", name="GPT-4o", provider="openai", max_tokens=8192),
    ]

    def get_model(self, model_id: str) -> Optional[LLMModelConfig]:
        """Get model config by ID. Returns None if not found."""
        for m in self.models:
            if m.id == model_id:
                return m
        return None

    def get_default_model(self) -> LLMModelConfig:
        """Get the default model config."""
        model = self.get_model(self.default_model)
        if model:
            return model
        # Fallback to first model if default not found
        return self.models[0]

    def list_available_models(self) -> List[dict]:
        """List models available for frontend selection."""
        return [
            {"id": m.id, "name": m.name, "provider": m.provider}
            for m in self.models
        ]


class MemorySummarizationConfig(BaseModel):
    """Configuration for the background memory summarization LLM call."""
    model: str = "gpt-4o-mini"         # Model ID (references llm.models)
    temperature: float = 0.3
    max_tokens: int = 2000


class Settings(BaseSettings):
    """Agent Service settings.
    Loaded from conf/agent-service.yaml, overridable by env vars.
    """

    # Server configuration
    ws_port: int = 8001
    grpc_port: int = 50051
    host: str = "0.0.0.0"

    # LLM configuration (multi-model)
    llm: LLMConfig = LLMConfig()

    # Memory summarization (background context compression)
    memory_summarization: MemorySummarizationConfig = MemorySummarizationConfig()

    # Service endpoints
    sqlmesh_service_url: str = "http://localhost:8002"
    workspace_service_url: str = "localhost:50053"

    # Database for chat history & session persistence
    # Default: embedded SQLite (zero external dependencies).
    database_type: str = "sqlite"             # "sqlite", "mysql", or "postgresql"
    database_sqlite_path: str = ".data/agenticdata.db"  # Relative to project root
    database_host: str = "localhost"
    database_port: int = 3306
    database_name: str = "agenticdata"
    database_username: str = ""
    database_password: str = ""

    @property
    def database_url(self) -> str:
        """Construct database connection URL based on configured type."""
        project_root = Path(__file__).parent.parent.parent
        return build_database_url(
            db_type=self.database_type,
            sqlite_path=self.database_sqlite_path,
            host=self.database_host,
            port=self.database_port,
            name=self.database_name,
            username=self.database_username,
            password=self.database_password,
            project_root=project_root,
            sqlite_default=".data/agenticdata.db",
        )

    # Agent configuration
    max_tool_call_timeout_seconds: int = 600
    # Per-message turn ceiling. Multi-agent handoffs + parallel tool calls + retries
    # consume turns fast; 20 is a balanced fallback. Overridable via conf/.agent config.
    max_turns_per_message: int = 20

    # Shell tool: auto-install Python 3.13 when not found locally.
    # SECURITY: Disabled by default. Enabling allows the agent service to run
    # system package manager commands (brew/apt-get/dnf/yum/winget) and a
    # `curl | bash` Homebrew bootstrap at runtime — a significant RCE surface.
    # Only enable in trusted, isolated environments.
    shell_auto_install_python: bool = False

    # Session cache: max number of ChatSessions kept in memory. When exceeded,
    # the oldest *inactive* sessions are evicted (LRU); an evicted session is
    # transparently reloaded from the DB on next access. Set <= 0 to disable
    # eviction (unbounded — not recommended for a long-running service).
    session_cache_max_size: int = 500

    # Workspace context cache: max number of workspaces whose rules/memory are
    # kept in memory. Oldest entries are evicted (LRU) when exceeded.
    workspace_context_cache_max_size: int = 100

    # Logging
    log_level: str = "INFO"

    model_config = {"env_prefix": "AGENT_SERVICE_"}

    def __init__(self, **kwargs):
        # Load YAML first, then let env vars override via pydantic-settings
        yaml_data = _load_yaml_config("agent-service.yaml")
        yaml_flat = _flatten_yaml(yaml_data)
        merged = {**yaml_flat, **kwargs}
        super().__init__(**merged)


def _env_id_suffix(model_id: str) -> str:
    """Normalize a model id into an env-var-safe suffix (UPPER, non-alnum -> '_')."""
    return "".join(c if c.isalnum() else "_" for c in model_id).upper()


def _apply_llm_env_overrides(models: List[LLMModelConfig]) -> None:
    """Apply environment-variable overrides on top of YAML-loaded LLM models.

    Both configuration styles are supported, so credentials can either live in
    conf/agent-service.yaml OR be injected via environment variables (env wins).
    Resolution order for each model's ``api_key`` / ``base_url`` (first match wins):

      1. Index-based:  AGENT_SERVICE_LLM_MODELS__<i>__API_KEY / __BASE_URL
      2. Id-based:     AGENT_SERVICE_LLM_API_KEY__<ID> / AGENT_SERVICE_LLM_BASE_URL__<ID>
                       (<ID> is the model id upper-cased with non-alnum chars -> '_')
      3. Global key fallback for any still-empty key:  AGENT_SERVICE_LLM_API_KEY

    A value present in the YAML file is kept when no matching env var is set.
    """
    global_key = os.getenv("AGENT_SERVICE_LLM_API_KEY")

    for i, model in enumerate(models):
        id_suffix = _env_id_suffix(model.id)

        api_key = (
            os.getenv(f"AGENT_SERVICE_LLM_MODELS__{i}__API_KEY")
            or os.getenv(f"AGENT_SERVICE_LLM_API_KEY__{id_suffix}")
        )
        if api_key:
            model.api_key = api_key

        base_url = (
            os.getenv(f"AGENT_SERVICE_LLM_MODELS__{i}__BASE_URL")
            or os.getenv(f"AGENT_SERVICE_LLM_BASE_URL__{id_suffix}")
        )
        if base_url:
            model.base_url = base_url

        # Global fallback only fills a key that is still empty (YAML + per-model env).
        if not model.api_key and global_key:
            model.api_key = global_key


def _flatten_yaml(data: dict) -> dict:
    """Flatten nested YAML config to match pydantic field names."""
    scalar_mapping = {
        "server.ws_port": "ws_port",
        "server.grpc_port": "grpc_port",
        "server.host": "host",
        "services.sqlmesh_service_url": "sqlmesh_service_url",
        "services.workspace_service_url": "workspace_service_url",
        "database.type": "database_type",
        "database.sqlite_path": "database_sqlite_path",
        "database.host": "database_host",
        "database.port": "database_port",
        "database.name": "database_name",
        "database.username": "database_username",
        "database.password": "database_password",
        "agent.max_tool_call_timeout_seconds": "max_tool_call_timeout_seconds",
        "agent.max_turns_per_message": "max_turns_per_message",
        "agent.shell_auto_install_python": "shell_auto_install_python",
        "agent.session_cache_max_size": "session_cache_max_size",
        "agent.workspace_context_cache_max_size": "workspace_context_cache_max_size",
        "logging.level": "log_level",
    }
    result = flatten_yaml_mapping(data, scalar_mapping)

    # LLM multi-model config (complex nested structure)
    llm_data = data.get("llm")
    if isinstance(llm_data, dict):
        llm_models = []
        for m in llm_data.get("models", []):
            llm_models.append(LLMModelConfig(**m))
        _apply_llm_env_overrides(llm_models)
        result["llm"] = LLMConfig(
            default_model=llm_data.get("default_model", "gpt-4o"),
            models=llm_models or [LLMModelConfig(id="gpt-4o", name="GPT-4o", provider="openai")],
        )

    # Memory summarization config (nested under agent.memory.summarization)
    agent_data = data.get("agent", {})
    if isinstance(agent_data, dict):
        memory_data = agent_data.get("memory", {})
        if isinstance(memory_data, dict):
            summ_data = memory_data.get("summarization", {})
            if isinstance(summ_data, dict):
                result["memory_summarization"] = MemorySummarizationConfig(**summ_data)

    return result


settings = Settings()
