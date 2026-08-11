"""Map agent display names (SDK Agent.name) back to AgentRegistry keys."""

from __future__ import annotations

from functools import lru_cache

from src.agents.factory import load_agent_config
from src.agents.registry import AgentRegistry


@lru_cache(maxsize=1)
def _display_to_registry_key() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in AgentRegistry.list_agents():
        mapping[key] = key
        try:
            cfg = load_agent_config(key)
            display = cfg.get("display_name") or cfg.get("name") or key
            mapping[str(display)] = key
        except FileNotFoundError:
            continue
    return mapping


def clear_agent_resolver_cache() -> None:
    """Invalidate the display-name → registry-key mapping.

    Call after adding/removing agent YAMLs at runtime or after
    ``clear_config_cache()`` so new display names are picked up.
    """
    _display_to_registry_key.cache_clear()


def registry_key_for_agent_name(name: str, *, fallback: str = "triage") -> str:
    """Resolve an SDK agent name to a registry key."""
    if not name:
        return fallback
    return _display_to_registry_key().get(name, fallback)
