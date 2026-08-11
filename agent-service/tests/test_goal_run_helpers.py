"""Tests for goal-run helpers (turn accounting)."""

from __future__ import annotations

from types import SimpleNamespace

from src.runtime.streaming import resolve_segment_turns_used


def test_resolve_segment_turns_used_prefers_current_turn():
    result = SimpleNamespace(current_turn=7, new_items=[], is_complete=True)
    assert resolve_segment_turns_used(result, allocated_max_turns=30) == 7


def test_resolve_segment_turns_used_caps_at_allocated_budget():
    result = SimpleNamespace(current_turn=25, new_items=[], is_complete=False)
    assert resolve_segment_turns_used(result, allocated_max_turns=20) == 20


def test_resolve_segment_turns_used_falls_back_to_new_items():
    result = SimpleNamespace(current_turn=0, new_items=[1, 2, 3], is_complete=True)
    assert resolve_segment_turns_used(result, allocated_max_turns=30) == 3
