"""Approval hook binding: level → SDK needs_approval (single source of truth).

Regression guard for the bug where the registry-derived approval policy never
reached the SDK: tools showed "L1" badges yet still popped approval cards
(the SDK default), because ``needs_approval`` was attached to the wrong
object and silently ignored.
"""

import pytest

from src.tools import list_dir as list_dir_module
from src.tools.registry import DEFAULT_CONFIRMATION_THRESHOLD, ToolRegistry


class _FakeContext:
    """Minimal stand-in for RunContextWrapper: just a .context payload."""

    def __init__(self, require_confirmation_above: int | None):
        if require_confirmation_above is not None:
            self.context = type(
                "Ctx", (), {"require_confirmation_above": require_confirmation_above}
            )()
        else:
            self.context = None


class _FakeSdkTool:
    """Mimics FunctionTool: approval hooks bind to objects with on_invoke_tool."""

    def __init__(self):
        self.on_invoke_tool = self._invoke

    async def _invoke(self, *_args, **_kwargs) -> str:
        return ""


def test_list_dir_has_registry_approval_hook():
    tool = list_dir_module.list_dir
    # The SDK tool object (not the raw coroutine) must carry our callback.
    assert hasattr(tool, "on_invoke_tool"), "expected an SDK FunctionTool"
    bound = getattr(tool, "needs_approval", None)
    assert bound is not None and bound is not False
    assert getattr(bound, "_tool_registry_name", None) == "list_dir"


async def test_l1_tool_does_not_need_approval():
    bound = list_dir_module.list_dir.needs_approval
    # Default threshold (3) — L1 must pass without approval.
    assert await bound(_FakeContext(None), {}, "call-1") is False
    # Session-level threshold of 3 — still no approval for L1.
    assert await bound(_FakeContext(3), {}, "call-2") is False


async def test_threshold_lowered_below_level_requires_approval():
    bound = list_dir_module.list_dir.needs_approval
    # A workspace may tighten the threshold to 1: then even L1 asks.
    assert await bound(_FakeContext(1), {}, "call-3") is True


async def test_override_requires_reason_and_forces_approval():
    with pytest.raises(ValueError):
        ToolRegistry.register(
            "approval_test_tool", level=1, approval_override=True
        )(_FakeSdkTool())

    tool = _FakeSdkTool()
    ToolRegistry.register(
        "approval_test_tool",
        level=1,
        approval_override=True,
        approval_reason="sensitive data",
    )(tool)
    try:
        approval_fn = getattr(tool, "needs_approval", None)
        assert getattr(approval_fn, "_tool_registry_name", None) == (
            "approval_test_tool"
        )
        # Override forces approval regardless of threshold/level.
        assert await approval_fn(_FakeContext(None), {}, "call-4") is True
    finally:
        del ToolRegistry._tools["approval_test_tool"]


async def test_plain_l1_callback_respects_threshold():
    # Direct check of the level-vs-threshold comparison the hook encodes.
    tool = _FakeSdkTool()
    ToolRegistry.register("approval_test_tool_l1", level=1)(tool)
    try:
        fn = tool.needs_approval
        assert await fn(_FakeContext(3), {}, "c") is False
        assert await fn(_FakeContext(1), {}, "c") is True
    finally:
        del ToolRegistry._tools["approval_test_tool_l1"]


def test_default_threshold_semantics():
    # L1 < 3 < 5: only levels at/above the threshold need confirmation.
    assert ToolRegistry.get_tool_level("list_dir") == 1
    assert not ToolRegistry.requires_confirmation("list_dir")
    assert DEFAULT_CONFIRMATION_THRESHOLD == 3
