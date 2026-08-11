"""Data Engineering Agent — SQLMesh data model development specialist.

Configuration is defined in subagents/data_engineering_agent.yaml (single source of truth).
This module only registers the factory function with AgentRegistry.
"""

import typing as t

from agents import Agent

from src.agents.registry import AgentRegistry
from src.agents.factory import create_agent


@AgentRegistry.register("data_engineering_agent")
def create_data_engineering_agent(
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
    """Create the Data Engineering Agent from YAML configuration.

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
        name="data_engineering_agent",
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
