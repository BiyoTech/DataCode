"""Asset Orchestration Agent — Dagster asset orchestration code specialist.

Configuration is defined in subagents/asset_orchestration_agent.yaml (single
source of truth). This module only registers the factory function with
AgentRegistry.
"""

import typing as t

from agents import Agent
from src.agents.factory import create_agent
from src.agents.registry import AgentRegistry


@AgentRegistry.register("asset_orchestration_agent")
def create_asset_orchestration_agent(
    workspace_path: str = "",
    mode: str = "ask",
    rules: t.Optional[t.List[str]] = None,
    memory_index: str = "",
    environment: str = "prod",
    model: t.Any = None,
    user_id: str = "",
    session_id: str = "",
    **kwargs,
) -> Agent:
    """Create the Asset Orchestration Agent from YAML configuration.

    Args:
        workspace_path: Current workspace path.
        mode: Interaction mode (ask/craft/plan).
        rules: Rule contents loaded from .agent/rules/.
        memory_index: L1 memory index for prompt injection.
        environment: Active SQLMesh environment from toolbar picker.
        model: LLM model instance or name string.
        user_id: Current user identifier (propagated to runtime tools).
        session_id: Current session identifier (required for command-level
            approval SSE routing in the shell tool).
    """
    return create_agent(
        name="asset_orchestration_agent",
        workspace_path=workspace_path,
        mode=mode,
        rules=rules,
        memory_index=memory_index,
        environment=environment,
        model=model,
        user_id=user_id,
        session_id=session_id,
        execution_style=kwargs.get("execution_style", "agent"),
    )
