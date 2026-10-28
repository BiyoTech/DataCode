"""Tool Registry — dynamic tool registration with risk-level metadata.

Each tool is registered via the ``@ToolRegistry.register`` decorator, which
stores the callable together with its metadata (risk level, description).

The **agent-to-tool mapping is NOT maintained here** — that relationship is
defined exclusively by the ``tools:`` list in each agent's YAML configuration
(see ``src/subagents/*.yaml`` and ``src/agents/factory.py``).

Risk model (single source of truth):
  - Each tool declares its risk level (1-6) at registration time via ``level``.
  - If ``level`` is omitted, ``DEFAULT_TOOL_LEVEL`` (1 = read-only) is used.
  - At runtime, ``confirmation_threshold`` determines whether user approval is
    needed: ``tool.level >= threshold`` → human approval required.
  - The SDK-level ``needs_approval`` hook is *derived* from ``level`` and
    injected by ``register()`` — tool authors never write it by hand, so
    "low level but asks for approval" style drift is structurally
    impossible. The only sanctioned escape hatch is
    ``approval_override=True`` (+ mandatory reason) on ``register()``.
  - Badge text and tone are computed by ``risk_badge_for_level()``.

Level definitions:
  L1 = Read      Read-only queries
  L2 = Write     File writes
  L3 = Plan      Non-destructive planning operations
  L4 = Apply     Destructive planning operations
  L5 = Deploy    Environment changes
  L6 = Admin     Administrative operations
"""

import typing as t

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Tool callable type (openai-agents SDK function tools)
ToolCallable = t.Any  # agents.function_tool decorated functions

# Fallback level when a tool does not declare one (L1 = read-only, safest)
DEFAULT_TOOL_LEVEL = 1

# Confirmation threshold: tool.level >= this value requires user approval
DEFAULT_CONFIRMATION_THRESHOLD = 3

# Risk level → badge tone mapping
_LEVEL_TONE_MAP: t.Dict[str, t.Tuple[int, int]] = {
    "low": (1, 2),      # L1-L2: green
    "medium": (3, 3),   # L3: yellow
    "high": (4, 6),     # L4-L6: red
}


def risk_badge_for_level(level: int) -> t.Tuple[str, str]:
    """Compute the risk badge (label, tone) for a given tool level.

    This is the single mapping point from ``tool.level`` to front-end display
    information. All code that needs to show a risk badge should call this
    function rather than computing it independently.

    Args:
        level: Tool risk level (1-6).

    Returns:
        ``(label, tone)`` tuple where *label* is e.g. ``"L3"`` and *tone*
        is one of ``"low"`` / ``"medium"`` / ``"high"``.
    """
    label = f"L{level}"
    if level >= 4:
        tone = "high"
    elif level >= 3:
        tone = "medium"
    else:
        tone = "low"
    return (label, tone)


class ToolRegistry:
    """Registry for dynamically registered tools and their metadata."""

    # Map: tool_name -> (tool_callable, metadata)
    _tools: t.Dict[str, t.Tuple[ToolCallable, t.Dict[str, t.Any]]] = {}

    @classmethod
    def register(
        cls,
        name: str,
        level: int = DEFAULT_TOOL_LEVEL,
        description: str = "",
        approval_override: bool = False,
        approval_reason: str = "",
    ) -> t.Callable:
        """Decorator — register a tool function with metadata.

        Args:
            name: Tool name identifier.
            level: Risk level (1=read, 2=file write, 3=Plan, 4=Apply,
                   5=Deploy, 6=Admin). **Single source of truth** for
                   approval: the SDK-level ``needs_approval`` callback is
                   derived from ``level`` and injected here, so tool authors
                   never write it by hand.
            description: Human-readable description (for UI display).
            approval_override: Escape hatch — force approval for this tool
                   even below the threshold. Must be paired with
                   *approval_reason*; overrides are logged for auditability.
            approval_reason: Mandatory when *approval_override* is True.

        Usage::

            @ToolRegistry.register("schema_discovery", level=1)
            @function_tool
            async def schema_discovery(...) -> str:
                ...
        """
        if approval_override and not approval_reason.strip():
            raise ValueError(
                f"Tool '{name}' uses approval_override without a reason; "
                "an auditable reason is required."
            )

        def decorator(func: ToolCallable) -> ToolCallable:
            metadata = {
                "level": level,
                "description": description,
                "approval_override": approval_override,
            }
            cls._tools[name] = (func, metadata)

            # Inject the approval decision derived from `level` into the
            # already-constructed SDK tool object. In openai-agents,
            # `needs_approval` is a mutable dataclass field on FunctionTool
            # (read by the run loop); older layouts kept it on the
            # on_invoke_tool wrapper, so we set both for compatibility.
            approval_fn = cls._build_needs_approval(
                name, override=approval_override
            )
            cls._attach_needs_approval(func, approval_fn, name)

            if approval_override:
                logger.warning(
                    "approval_override_declared",
                    tool_name=name,
                    reason=approval_reason,
                )
            logger.info("tool_registered", tool_name=name, level=level)
            return func
        return decorator

    @classmethod
    def register_metadata(
        cls,
        name: str,
        level: int = DEFAULT_TOOL_LEVEL,
        description: str = "",
    ) -> None:
        """Register tool metadata only (without a callable).

        Some tools are constructed dynamically at agent build time (e.g. the
        shell tool, which needs workspace_path for cwd isolation and therefore
        cannot be registered as a module-level singleton). Registering their
        metadata here ensures:

          - ``list_tools()`` / ``/api/v1/tools`` includes them for UI display.
          - ``get_tool_level(name)`` returns the correct risk level instead of
            the default, so approval/risk-badge logic works consistently.

        The callable slot is set to ``None``; callers that need the actual tool
        instance must obtain it via the agent factory, not the registry.
        """
        metadata = {
            "level": level,
            "description": description,
        }
        cls._tools[name] = (None, metadata)
        logger.info("tool_metadata_registered", tool_name=name, level=level)

    @classmethod
    def _build_needs_approval(
        cls, tool_name: str, override: bool = False
    ) -> t.Callable[..., t.Awaitable[bool]]:
        """Build the approval-check function for a tool.

        Logic: ``tool.level >= confirmation_threshold`` → approval required.
        The threshold is read from ``run_context.context`` first (supports
        session-level override), falling back to ``DEFAULT_CONFIRMATION_THRESHOLD``.

        If *override* is set, approval is always required (audited escape
        hatch for e.g. sensitive-data tools below the threshold).
        """

        async def _needs_approval(
            run_context: t.Any, tool_parameters: t.Dict[str, t.Any], call_id: str
        ) -> bool:
            del tool_parameters, call_id
            if override:
                return True
            tool_level = cls.get_tool_level(tool_name)
            context = getattr(run_context, "context", None)
            threshold = getattr(
                context,
                "require_confirmation_above",
                DEFAULT_CONFIRMATION_THRESHOLD,
            )
            return tool_level >= threshold

        # Mark the callback so _verify_approval_bindings can recognize it.
        _needs_approval._tool_registry_name = tool_name  # type: ignore[attr-defined]
        return _needs_approval

    @classmethod
    def _attach_needs_approval(
        cls, func: ToolCallable, approval_fn: t.Callable, tool_name: str
    ) -> bool:
        """Attach *approval_fn* as the SDK approval hook on *func*.

        The decorator stack means ``func`` is an already-built SDK tool object
        (e.g. ``FunctionTool``). Returns True if at least one attachment
        point was found; logs a warning otherwise so the mismatch surfaces at
        import time rather than as a stray approval card at run time.
        """
        targets: t.List[t.Any] = []
        if hasattr(func, "on_invoke_tool"):
            targets.append(func)
            invoke = getattr(func, "on_invoke_tool", None)
            if invoke is not None and invoke is not func:
                targets.append(invoke)
        elif hasattr(func, "needs_approval"):
            targets.append(func)

        if not targets:
            logger.warning(
                "approval_hook_not_found",
                tool_name=tool_name,
                hint=(
                    "Registered object is not an SDK tool; approval policy "
                    "cannot be enforced for it."
                ),
            )
            return False

        attached = False
        for target in targets:
            try:
                target.needs_approval = approval_fn
                attached = True
            except (AttributeError, TypeError):
                # e.g. a bound-method on_invoke_tool wrapper that rejects
                # attribute assignment; other targets may still work.
                continue
        if not attached:
            logger.warning(
                "approval_hook_readonly",
                tool_name=tool_name,
            )
        return attached

    @classmethod
    def _verify_approval_bindings(cls) -> None:
        """Startup check: every registered SDK tool carries our approval hook.

        Catches regressions where a tool reaches the agent without a
        registry-derived ``needs_approval`` (which would silently fall back
        to the SDK default instead of the level-based policy).
        """
        for name, (tool, _meta) in cls._tools.items():
            if tool is None or not hasattr(tool, "on_invoke_tool"):
                continue
            bound = getattr(tool, "needs_approval", None)
            expected = getattr(bound, "_tool_registry_name", None)
            if expected != name:
                logger.warning(
                    "approval_binding_mismatch",
                    tool_name=name,
                    bound_type=type(bound).__name__ if bound is not None else "None",
                )

    @classmethod
    def get_tools_by_level(cls, max_level: int) -> t.List[ToolCallable]:
        """Return all tools whose level does not exceed *max_level*.

        Note: kept for diagnostic / management purposes. Agent tool
        authorization is determined solely by the YAML ``tools`` list, not
        by level filtering. The ``level`` is only used for the runtime
        confirmation threshold (see ``_build_needs_approval``).

        Metadata-only registrations (callable is None, e.g. the shell tool)
        are skipped here since they have no callable to return.
        """
        return [
            tool for tool, meta in cls._tools.values()
            if tool is not None and meta["level"] <= max_level
        ]

    @classmethod
    def get_tool_metadata(cls, tool_name: str) -> t.Optional[t.Dict[str, t.Any]]:
        """Return the metadata dict for *tool_name*, or ``None`` if unknown."""
        if tool_name in cls._tools:
            return cls._tools[tool_name][1]
        return None

    @classmethod
    def get_tool_level(cls, tool_name: str, default: int = DEFAULT_TOOL_LEVEL) -> int:
        """Get the risk level of a tool.

        Default-value policy:
          - Tool registered but no ``level`` declared → ``DEFAULT_TOOL_LEVEL`` (1)
          - Tool not registered → caller-supplied *default*
        """
        metadata = cls.get_tool_metadata(tool_name)
        if not metadata:
            return default
        return int(metadata.get("level", default))

    @classmethod
    def get_tool_risk_badge(
    cls, tool_name: str, default: int = DEFAULT_TOOL_LEVEL
) -> t.Tuple[str, str]:
        """Convenience: ``risk_badge_for_level(get_tool_level(tool_name))``."""
        level = cls.get_tool_level(tool_name, default=default)
        return risk_badge_for_level(level)

    @classmethod
    def get_confirmation_threshold(cls) -> int:
        """Return the default confirmation threshold.

        Tools with ``level >= threshold`` require user approval before execution.
        """
        return DEFAULT_CONFIRMATION_THRESHOLD

    @classmethod
    def requires_confirmation(cls, tool_name: str, threshold: t.Optional[int] = None) -> bool:
        """Check whether *tool_name* requires user confirmation.

        Args:
            tool_name: Tool name.
            threshold: Custom threshold; ``None`` uses the default.

        Returns:
            ``True`` if the tool must be approved before execution.
        """
        effective_threshold = threshold if threshold is not None else DEFAULT_CONFIRMATION_THRESHOLD
        return cls.get_tool_level(tool_name) >= effective_threshold

    @classmethod
    def list_tools(cls) -> t.List[t.Dict[str, t.Any]]:
        """List all registered tools with their metadata."""
        return [
            {"name": name, **metadata}
            for name, (_, metadata) in cls._tools.items()
        ]

    @classmethod
    def has_tool(cls, name: str) -> bool:
        """Check whether a tool is registered."""
        return name in cls._tools


# Import tool modules to trigger @register decorators
def load_all_tools() -> None:
    """Import every tool module so its @ToolRegistry.register decorator fires.

    Also registers metadata-only entries for tools that are built dynamically
    at agent construction time (e.g. ``shell``) so their risk level and
    description are available for UI display and approval-threshold logic.
    """
    from src.tools import (  # noqa: F401
        apply_patch,
        create_file,
        get_lineage,
        list_dir,
        list_models,
        memory_read,
        memory_summarize_session,
        memory_write_index,
        memory_write_summary,
        query,
        read_file,
        schema_discovery,
        search_code,
        sqlmesh_docs,
        write_todo,
    )

    # The shell tool is constructed per-agent (needs workspace_path), so it
    # cannot use the @register decorator. Register its metadata here so the
    # risk level (L5 = Deploy) is visible to the UI and approval threshold.
    if not ToolRegistry.has_tool("shell"):
        ToolRegistry.register_metadata(
            "shell",
            level=5,
            description=(
    "Execute shell commands in the agent workspace "
    "(subject to command policy)."
),
        )

    # Verify that every SDK tool carries the registry-derived approval hook.
    # A mismatch here means the level-based approval policy is silently
    # bypassed, so surface it before the service accepts traffic.
    ToolRegistry._verify_approval_bindings()

    logger.info("all_tools_loaded", count=len(ToolRegistry._tools))
