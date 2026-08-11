"""Handoff input filter registry — named filters + mode-level handoff gating.

Aligns with the ToolRegistry / AgentRegistry pattern in this project. Filters are
pure HandoffInputData -> HandoffInputData functions registered by name via decorator.
The registry is resolved at agent construction time from YAML ``handoff_context``.

Key design:
- ``_safe_filter_wrapper`` wraps every filter so exceptions never crash the run;
  they fall back to the original inputs with a warning log.
- ``build_mode_gate`` produces an ``is_enabled`` callback that reads the current
  mode from ``RunContextWrapper.context.mode`` and compares against a YAML-defined
  allowlist.
- ``resolve_handoff_config`` merges the global ``default_filter`` with per-target
  overrides, returning a ``(HandoffInputFilter | None, is_enabled_callable)`` pair.
"""

from __future__ import annotations

import typing as t

from agents.extensions.handoff_filters import remove_all_tools
from agents.handoffs import HandoffInputData, HandoffInputFilter

from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

HandoffFilterFactory = t.Callable[[], HandoffInputFilter]


class HandoffFilterRegistry:
    """Registry of named handoff input filters (mirrors ToolRegistry style)."""

    _filters: t.Dict[str, HandoffFilterFactory] = {}

    @classmethod
    def register(cls, name: str) -> t.Callable[[HandoffFilterFactory], HandoffFilterFactory]:
        """Decorator: register a filter factory under a string name.

        Usage::

            @HandoffFilterRegistry.register("my_filter")
            def my_filter() -> HandoffInputFilter:
                def _filter(data): ...
                return _filter
        """

        def decorator(factory: HandoffFilterFactory) -> HandoffFilterFactory:
            cls._filters[name] = factory
            logger.info("handoff_filter_registered", name=name)
            return factory

        return decorator

    @classmethod
    def get(cls, name: str) -> HandoffInputFilter:
        """Look up a filter by name. Raises KeyError if not found."""
        if name not in cls._filters:
            raise KeyError(
                f"Handoff filter '{name}' not registered. "
                f"Available: {list(cls._filters.keys())}"
            )
        return cls._filters[name]()

    @classmethod
    def has(cls, name: str) -> bool:
        """Check whether a filter name is registered."""
        return name in cls._filters

    @classmethod
    def list_filters(cls) -> t.List[str]:
        """Return all registered filter names."""
        return list(cls._filters.keys())


# ---------------------------------------------------------------------------
# Built-in filters
# ---------------------------------------------------------------------------

@HandoffFilterRegistry.register("none")
def _passthrough_filter() -> HandoffInputFilter:
    """No-op filter — forwards all items unchanged."""

    def _filter(data: HandoffInputData) -> HandoffInputData:
        return data

    return _filter


@HandoffFilterRegistry.register("remove_tools")
def _remove_tools_filter() -> HandoffInputFilter:
    """Strip all tool-call / tool-output / reasoning items from handoff input.

    Delegates to the SDK built-in ``remove_all_tools`` which also preserves
    the ``input_items`` / ``new_items`` separation so the session history
    stays complete while the model input is trimmed.
    """
    return remove_all_tools


# ---------------------------------------------------------------------------
# Safe wrapper — graceful degradation on filter failure
# ---------------------------------------------------------------------------

def _safe_filter_wrapper(
    inner: HandoffInputFilter,
    filter_name: str,
) -> HandoffInputFilter:
    """Wrap a filter so exceptions fall back to original data instead of crashing.

    The SDK default behaviour is to ``raise UserError`` when a filter fails.
    For a production chat system we prefer graceful degradation: log a warning
    and return the unfiltered ``HandoffInputData``.
    """

    def _wrapped(data: HandoffInputData) -> HandoffInputData:
        try:
            return inner(data)
        except Exception:
            logger.warning(
                "handoff_filter_failed_falling_back",
                filter=filter_name,
                history_items=(
                    len(data.input_history)
                    if isinstance(data.input_history, tuple)
                    else "(str)"
                ),
                pre_items=len(data.pre_handoff_items),
                new_items=len(data.new_items),
                exc_info=True,
            )
            return data

    return _wrapped


# ---------------------------------------------------------------------------
# Mode gate — is_enabled callback builder
# ---------------------------------------------------------------------------

def build_mode_gate(
    agent_name: str,
    enabled_modes: t.Sequence[str],
) -> t.Callable[[t.Any, t.Any], bool]:
    """Return an ``is_enabled`` callable for ``handoff()``.

    The callback reads ``RunContextWrapper.context.mode`` and returns True
    only when the current mode is in ``enabled_modes``.  Both arguments
    (run_context, agent) are accepted for SDK compatibility.

    Args:
        agent_name: Name of the target agent (for logging only).
        enabled_modes: Modes where the handoff is permitted, e.g.
            ``["craft", "all"]``.
    """

    allowed = frozenset(enabled_modes)

    def _gate(ctx: t.Any, _agent: t.Any) -> bool:
        # Read mode from our ToolContext, which carries session-level metadata
        mode = getattr(ctx, "context", None)
        if mode is not None:
            mode = getattr(mode, "mode", None)
        current = mode or "ask"
        enabled = current in allowed
        if not enabled:
            logger.debug(
                "handoff_disabled_by_mode",
                target=agent_name,
                mode=current,
                allowed_modes=sorted(allowed),
            )
        return enabled

    return _gate


# ---------------------------------------------------------------------------
# YAML → filter resolution
# ---------------------------------------------------------------------------

# System default when no handoff_context is configured in YAML
_SYSTEM_DEFAULT_FILTER = "remove_tools"


def resolve_handoff_config(
    handoff_context: t.Dict[str, t.Any] | None,
    target_name: str,
) -> tuple[HandoffInputFilter | None, t.Any]:
    """Merge global defaults and per-target overrides into a ``(filter, is_enabled)`` pair.

    Resolution order (later wins):
    1. System default (``remove_tools``) when ``handoff_context`` is entirely absent.
    2. ``handoff_context.default_filter`` — global filter name.
    3. ``handoff_context.targets.<name>.filter`` — per-target override (can be ``"none"``
       to disable filtering for a specific handoff).
    4. ``handoff_context.targets.<name>.enabled_modes`` — per-target mode allowlist;
       if unspecified, handoff is always enabled.

    Returns:
        ``(HandoffInputFilter | None, is_enabled_callable)``.
        The filter is ``None`` when the resolved name is ``"none"``.
    """

    ctx = handoff_context or {}
    target_cfg = (ctx.get("targets") or {}).get(target_name, {})

    # Resolve filter name
    filter_name = target_cfg.get("filter") or ctx.get("default_filter") or _SYSTEM_DEFAULT_FILTER

    input_filter: HandoffInputFilter | None = None
    if filter_name != "none":
        try:
            raw_filter = HandoffFilterRegistry.get(filter_name)
            input_filter = _safe_filter_wrapper(raw_filter, filter_name)
        except KeyError:
            logger.warning(
                "handoff_filter_not_found_fallback_to_remove_tools",
                requested=filter_name,
                target=target_name,
            )
            input_filter = _safe_filter_wrapper(
                HandoffFilterRegistry.get("remove_tools"), "remove_tools"
            )

    # Resolve mode gate
    enabled_modes = target_cfg.get("enabled_modes", [])
    if enabled_modes:
        is_enabled = build_mode_gate(target_name, enabled_modes)
    else:
        is_enabled = True

    return input_filter, is_enabled
