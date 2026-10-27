"""Configuration loading utilities."""

from datacode_common.config.mapping_guard import (
    dropped_yaml_keys,
    unknown_field_names,
    yaml_leaf_keys,
)
from datacode_common.config.platform import (
    DatabaseConfig,
    DatabaseServiceConfig,
    LoggingConfig,
    PlatformConfig,
    ServiceTopology,
    WorkspaceConfig,
    platform_config,
)
from datacode_common.config.yaml_loader import (
    drop_env_provided_keys,
    find_config_file,
    flatten_yaml_mapping,
    load_yaml_config,
)

__all__ = [
    # YAML loader
    "drop_env_provided_keys",
    "find_config_file",
    "flatten_yaml_mapping",
    "load_yaml_config",
    # Mapping guard (every shipped YAML key must reach Settings)
    "dropped_yaml_keys",
    "unknown_field_names",
    "yaml_leaf_keys",
    # Platform config
    "DatabaseConfig",
    "DatabaseServiceConfig",
    "LoggingConfig",
    "PlatformConfig",
    "ServiceTopology",
    "WorkspaceConfig",
    "platform_config",
]
