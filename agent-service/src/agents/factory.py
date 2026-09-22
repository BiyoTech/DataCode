"""Unified Agent Factory — builds Agent instances from YAML configuration.

YAML is the single source of truth for agent definitions. This factory:
1. Loads and parses YAML config files from the subagents directory.
2. Renders system_prompt templates with runtime context variables.
3. Resolves tools from ToolRegistry by name (YAML `tools` list is the sole
   authority for which tools an Agent can use — no level-based filtering).
4. Recursively resolves handoff agents.
5. Constructs openai-agents SDK Agent instances.

Tool risk control is handled at execution time via `tool.level` and the
confirmation threshold (see ToolRegistry._build_needs_approval), NOT by
filtering the tool list during agent construction.

Template variables available in system_prompt:
  {workspace_path}  — current workspace path
  {mode}            — interaction mode (ask / craft / design)
  {mode_instructions} — mode-specific behavioral instructions
  {execution_instructions} — execution-style behavior (agent / interactive)
  {rules_section}   — workspace rules injected from .agent/rules/
  {memory_section}  — L1 memory index injected on session start
"""

import typing as t
from pathlib import Path

import yaml

from agents import Agent, handoff
from src.agents.handoff_filters import resolve_handoff_config
from src.context.execution_style import compose_execution_instructions
from src.skills.registry import SkillRegistry
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Directory containing agent YAML definitions
_SUBAGENTS_DIR = Path(__file__).parent.parent / "subagents"

# Cache: agent_name -> parsed YAML dict
_config_cache: t.Dict[str, t.Dict[str, t.Any]] = {}

# Maximum character lengths for system prompt sections to avoid context overflow
MAX_RULES_CHARS = 8000
MAX_MEMORY_CHARS = 4000

# Mode-specific instruction templates (shared across agents)
MODE_INSTRUCTIONS: t.Dict[str, str] = {
    "ask": (
        "**Ask Mode (Read-Only Preference)**:\n"
        "- Focus on answering questions and understanding the workspace\n"
        "- Do NOT create or modify files unless the workflow explicitly requires confirmation\n"
        "- Prefer read-only investigation and explanation"
    ),
    "craft": (
        "**Craft Mode (Implementation Mode)**:\n"
        "- You may implement requested changes in the workspace\n"
        "- Show planned impact before destructive actions when appropriate\n"
        "- Follow approval requirements for higher-risk operations"
    ),
    "design": (
        "**Design Mode (Design Only)**:\n"
        "- Investigate the workspace and produce an implementation design\n"
        "- Do NOT execute implementation or deployment actions\n"
        "- Only write design artifacts when the workflow explicitly requires it"
    ),
    "all": (
        "**All Modes**: This agent operates in all modes without restriction."
    ),
}


def load_agent_config(name: str) -> t.Dict[str, t.Any]:
    """Load and cache an agent's YAML configuration.

    The cache is invalidated automatically when the YAML file's mtime changes,
    so editing a prompt file no longer requires a service restart.

    Args:
        name: Agent name matching the YAML filename (without extension).

    Returns:
        Parsed YAML dict.

    Raises:
        FileNotFoundError: If the YAML config does not exist.
    """
    yaml_path = _SUBAGENTS_DIR / f"{name}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"Agent config not found: {yaml_path}. "
            f"Available: {[f.stem for f in _SUBAGENTS_DIR.glob('*.yaml')]}"
        )

    current_mtime = yaml_path.stat().st_mtime
    cached = _config_cache.get(name)
    if cached and cached.get("_source_mtime") == current_mtime:
        return cached

    with open(yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Store the mtime alongside the config so subsequent calls can detect
    # file changes without re-reading. The leading underscore keeps it
    # private — callers access config via normal dict keys.
    config["_source_mtime"] = current_mtime
    _config_cache[name] = config
    logger.info(
    "agent_config_loaded", agent=name, path=str(yaml_path),
    cache="miss" if cached else "init",
)
    return config


def _resolve_tools(
    tool_names: t.List[str],
) -> t.List[t.Any]:
    """Resolve tool callables from ToolRegistry by name list declared in YAML."""
    tools = []
    for tool_name in tool_names:
        if not ToolRegistry.has_tool(tool_name):
            logger.warning("tool_not_found", tool_name=tool_name)
            continue

        tool_callable = ToolRegistry._tools[tool_name][0]
        tools.append(tool_callable)

    return tools



def _render_system_prompt(
    template: str,
    workspace_path: str = "",
    mode: str = "ask",
    rules: t.Optional[t.List[str]] = None,
    memory_index: str = "",
    environment: str = "prod",
    skill_index: str = "",
    execution_style: str = "agent",
    execution_overlay: t.Optional[t.Any] = None,
) -> str:
    """Render a system_prompt template with runtime context variables.

    Uses str.format_map with a defaultdict-like fallback so missing
    placeholders don't raise KeyError.
    """
    rules_section = ""
    if rules:
        rules_section = "## Workspace Rules:\n\n"
        for rule_content in rules:
            rules_section += f"{rule_content}\n\n---\n\n"
        # Truncate to prevent context overflow
        if len(rules_section) > MAX_RULES_CHARS:
            rules_section = rules_section[:MAX_RULES_CHARS] + "\n\n...(rules truncated)"

    memory_section = ""
    if memory_index:
        memory_section = f"## Workspace Memory:\n\n{memory_index}"
        # Truncate to prevent context overflow
        if len(memory_section) > MAX_MEMORY_CHARS:
            memory_section = memory_section[:MAX_MEMORY_CHARS] + "\n...(memory truncated)"

    mode_instructions = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["all"])
    execution_instructions = compose_execution_instructions(
        execution_style, execution_overlay
    )

    # Use format_map with a SafeDict to handle missing keys gracefully
    variables = {
        "workspace_path": workspace_path or "(not set)",
        "mode": mode,
        "mode_instructions": mode_instructions,
        "execution_instructions": execution_instructions,
        "rules_section": rules_section,
        "memory_section": memory_section,
        "environment": environment or "prod",
        "skill_index": skill_index,
    }

    try:
        return template.format_map(_SafeFormatDict(variables))
    except (KeyError, ValueError) as e:
        logger.warning("prompt_render_error", error=str(e))
        return template


class _SafeFormatDict(dict):
    """Dict subclass that returns the placeholder itself for missing keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def create_agent(
    name: str,
    workspace_path: str = "",
    mode: str = "ask",
    rules: t.Optional[t.List[str]] = None,
    memory_index: str = "",
    environment: str = "prod",
    model: t.Any = None,
    user_id: str = "",
    session_id: str = "",
    execution_style: str = "agent",
    _resolving: t.Optional[t.Set[str]] = None,
) -> Agent:
    """Create an Agent instance from its YAML configuration.

    This is the single entry point for agent instantiation. All agent
    definitions (instructions, tools, handoffs, output_rules) are read
    from YAML. No agent-specific Python factory code is needed.

    Args:
        name: Agent name (must match a .yaml file in subagents/).
        workspace_path: Current workspace root path.
        mode: Interaction mode (ask / craft / plan / all).
        rules: Workspace rules from .agent/rules/.
        memory_index: L1 memory index content for prompt injection.
        model: LLM model instance or name string.
        _resolving: Internal set to detect circular handoff references.

    Returns:
        Configured Agent instance ready for use.
    """
    # Circular reference protection
    if _resolving is None:
        _resolving = set()
    if name in _resolving:
        logger.warning("circular_handoff_detected", agent=name)
        return Agent(name=name, instructions=f"Agent {name} (circular ref fallback)")
    _resolving.add(name)

    config = load_agent_config(name)

    # Resolve system prompt
    system_prompt = config.get("system_prompt", "")
    if not system_prompt:
        # If no explicit system_prompt in YAML, use display_name + description as fallback
        display_name = config.get("display_name", name)
        description = config.get("description", "")
        system_prompt = f"You are {display_name}. {description}"

    instructions = _render_system_prompt(
        template=system_prompt,
        workspace_path=workspace_path,
        mode=mode,
        rules=rules,
        memory_index=memory_index,
        environment=environment,
        execution_style=execution_style,
        execution_overlay=config.get("execution_overlay"),
    )

    # Append output_rules to instructions.
    # Use `or []` (not a get default) so an empty YAML key like `output_rules:`
    # — which PyYAML parses as None — is safely treated as an empty list.
    output_rules = config.get("output_rules") or []
    if output_rules:
        instructions += "\n\n## Output Rules:\n"
        for rule in output_rules:
            instructions += f"- {rule}\n"

    # Inject skill index for triage agent (entry point that routes to specialists)
    if name == "triage":
        skill_index = SkillRegistry.get_index_section()
        if skill_index:
            instructions += skill_index

    # Resolve tools by name from the YAML tools list.
    # Filter out tools that require runtime construction (e.g. shell tool needs
    # workspace_path at creation time, cannot be registered in ToolRegistry).
    # `or []` guards against an empty `tools:` YAML key (parsed as None).
    _runtime_tools = {"shell"}
    tool_names = config.get("tools") or []
    _resolvable_names = [tn for tn in tool_names if tn not in _runtime_tools]
    tools = _resolve_tools(_resolvable_names)

    # Inject shell tool at runtime (needs workspace_path for cwd isolation)
    if "shell" in tool_names:
        try:
            from src.tools.shell import create_native_shell_tool
            shell_tool = create_native_shell_tool(
                workspace_path,
                user_id=user_id,
                session_id=session_id,
                environment=environment,
            )
            tools.append(shell_tool)
            logger.info(
    "shell_tool_injected", agent=name, workspace_path=workspace_path,
    user_id=user_id, environment=environment,
)
        except Exception as e:
            # ERROR, not warning: the agent is still built, but WITHOUT the tool
            # its prompt tells it to use. The model then reports "I have no
            # terminal here" — a truthful answer to a broken assembly, and the
            # hardest kind of defect to trace back to a log line nobody reads.
            logger.error(
                "shell_tool_injection_failed",
                agent=name,
                workspace_path=workspace_path,
                error=str(e),
                hint="this agent will run without shell; commands will be "
                     "refused as unavailable",
            )

    # Resolve handoff agents recursively (skip background workers with exposed=false).
    # `or []` guards against an empty `handoffs:` YAML key: PyYAML parses a key
    # with no value as None, and `dict.get(key, [])` only applies the default
    # when the key is ABSENT — so a present-but-empty key would yield None and
    # crash the loop below with 'NoneType' object is not iterable.
    handoff_names = config.get("handoffs") or []
    # optional: dict with default_filter + targets
    handoff_context_cfg = config.get("handoff_context")
    handoffs = []
    for entry in handoff_names:
        # Support both plain string names and {name: ..., ...} dict entries
        if isinstance(entry, str):
            handoff_name = entry
        elif isinstance(entry, dict):
            handoff_name = entry.get("name", "")
        else:
            logger.warning("invalid_handoff_entry_type", type=type(entry).__name__)
            continue

        if not handoff_name:
            continue
        try:
            handoff_agent_config = load_agent_config(handoff_name)
            if handoff_agent_config.get("exposed") is False:
                logger.debug("skip_unexposed_handoff", agent=name, handoff=handoff_name)
                continue
            handoff_agent = create_agent(
                name=handoff_name,
                workspace_path=workspace_path,
                mode=mode,
                rules=rules,
                memory_index=memory_index,
                environment=environment,
                model=model,
                user_id=user_id,
                session_id=session_id,
                execution_style=execution_style,
                _resolving=_resolving.copy(),
            )

            # Resolve context filter + mode gate from YAML handoff_context
            input_filter, is_enabled = resolve_handoff_config(handoff_context_cfg, handoff_name)

            if input_filter is not None or is_enabled is not True:
                # Wrap with handoff() to inject filter + mode gating
                handoff_kwargs: t.Dict[str, t.Any] = {}
                if input_filter is not None:
                    handoff_kwargs["input_filter"] = input_filter
                if is_enabled is not True:
                    handoff_kwargs["is_enabled"] = is_enabled
                handoffs.append(handoff(handoff_agent, **handoff_kwargs))
                logger.debug(
                    "handoff_configured",
                    source=name,
                    target=handoff_name,
                    has_filter=input_filter is not None,
                    has_mode_gate=is_enabled is not True,
                )
            else:
                # Passthrough: no filter, no gate — keep bare Agent (SDK default wrapper)
                handoffs.append(handoff_agent)
        except FileNotFoundError:
            logger.warning("handoff_agent_not_found", agent=name, handoff=handoff_name)

    # Build the Agent
    agent_kwargs: t.Dict[str, t.Any] = {
        "name": config.get("display_name", name),
        "instructions": instructions,
        "tools": tools,
    }
    if handoffs:
        agent_kwargs["handoffs"] = handoffs
    if model:
        agent_kwargs["model"] = model

    agent = Agent(**agent_kwargs)
    logger.info(
        "agent_created",
        name=name,
        tools_count=len(tools),
        handoffs_count=len(handoffs),
    )
    return agent


def clear_config_cache() -> None:
    """Clear the loaded YAML config cache. Useful for tests or hot-reload."""
    _config_cache.clear()
    # Also clear the display-name → registry-key mapping so new agents
    # are picked up immediately after a cache flush.
    try:
        from src.runtime.agent_resolver import clear_agent_resolver_cache
        clear_agent_resolver_cache()
    except ImportError:
        pass
    logger.info("agent_config_cache_cleared")
