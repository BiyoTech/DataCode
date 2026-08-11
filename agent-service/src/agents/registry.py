"""Agent Registry — 可扩展的 Agent 注册和发现机制。

支持通过装饰器动态注册 Agent 工厂函数，
新增 Agent (Debug Agent, Review Agent 等) 无需修改核心框架。
"""

import typing as t
from agents import Agent

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Agent 工厂函数类型
AgentFactory = t.Callable[..., Agent]


class AgentRegistry:
    """Agent 注册表 — 管理 Agent 定义的可扩展注册。"""

    _agents: t.Dict[str, AgentFactory] = {}

    @classmethod
    def register(cls, name: str) -> t.Callable[[AgentFactory], AgentFactory]:
        """装饰器 — 注册 Agent 工厂函数。

        Usage:
            @AgentRegistry.register("data_engineering_agent")
            def create_data_engineering_agent(**kwargs) -> Agent:
                return Agent(name="Model Agent", ...)
        """
        def decorator(factory: AgentFactory) -> AgentFactory:
            cls._agents[name] = factory
            logger.info("agent_registered", agent_name=name)
            return factory
        return decorator

    @classmethod
    def get(cls, name: str, **kwargs: t.Any) -> Agent:
        """获取并实例化已注册的 Agent。

        Args:
            name: Agent 注册名称。
            **kwargs: 传递给 Agent 工厂的参数 (workspace_path, mode, rules 等)。

        Returns:
            实例化的 Agent。

        Raises:
            KeyError: Agent 名称未注册。
        """
        if name not in cls._agents:
            raise KeyError(
                f"Agent '{name}' not registered. Available: {list(cls._agents.keys())}"
            )
        return cls._agents[name](**kwargs)

    @classmethod
    def list_agents(cls) -> t.List[str]:
        """列出所有已注册的 Agent 名称。"""
        return list(cls._agents.keys())

    @classmethod
    def has(cls, name: str) -> bool:
        """检查 Agent 是否已注册。"""
        return name in cls._agents


def load_all_agents() -> None:
    """Import all agent modules to trigger @register decorators.

    Must be called at startup before any AgentRegistry.get() calls.
    Mirrors the pattern used by ToolRegistry.load_all_tools().
    """
    from src.agents import (  # noqa: F401
        triage,
        data_engineering_agent,
        data_analysis_agent,
        memory_agent,
        design_agent,
    )
    logger.info("all_agents_loaded", agents=list(AgentRegistry._agents.keys()))