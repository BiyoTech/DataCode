"""Data Analysis Agent — ad-hoc query, exploration, statistics, and visualization.

Configuration is defined in subagents/data_analysis_agent.yaml (single source of truth).
This module only registers the factory function with AgentRegistry.
"""

import typing as t

from agents import Agent

from src.agents.registry import AgentRegistry
from src.agents.factory import create_agent


@AgentRegistry.register("data_analysis_agent")
def create_data_analysis_agent(
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
    """Create the Data Analysis Agent from YAML configuration.

    Args:
        workspace_path: Current workspace path.
        mode: Interaction mode (ask/craft/design).
        rules: Rule contents loaded from .agent/rules/.
        memory_index: L1 memory index for prompt injection.
        environment: Active SQLMesh environment from toolbar picker.
        model: LLM model instance or name string.
        user_id: Current user identifier (propagated to runtime tools).
        session_id: Current session identifier (required for command-level
            approval SSE routing in the shell tool).
    """
    return create_agent(
        name="data_analysis_agent",
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
