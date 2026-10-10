"""``mapping_guard``: the helpers behind the per-service YAML coverage tests.

Behavioural, against plain dicts — each service's own test wires these to its
real YAML file and real Settings.
"""

from __future__ import annotations

from datacode_common.config.mapping_guard import (
    dropped_yaml_keys,
    unknown_field_names,
    yaml_leaf_keys,
)


class TestYamlLeafKeys:
    def test_scalars_and_nested_paths(self):
        data = {"server": {"port": 1}, "name": "x"}
        assert yaml_leaf_keys(data) == {"server.port", "name"}

    def test_a_list_is_a_leaf(self):
        # Consume whole ("llm.models"), never path into elements.
        data = {"llm": {"models": [{"id": "a"}, {"id": "b"}]}}
        assert yaml_leaf_keys(data) == {"llm.models"}

    def test_empty_mapping_has_no_leaves(self):
        assert yaml_leaf_keys({}) == frozenset()


class TestDroppedYamlKeys:
    def test_a_mapped_key_is_fine(self):
        data = {"task": {"backend": "local"}}
        assert dropped_yaml_keys(data, {"task.backend": "task_backend"}) == []

    def test_an_unmapped_key_is_reported(self):
        dropped = dropped_yaml_keys({"task": {"backend": "local"}}, {})
        assert dropped == ["task.backend"]

    def test_handled_prefixes_are_exempt(self):
        data = {"llm": {"default_model": "m", "models": []}, "agent": {"turns": 1}}
        assert dropped_yaml_keys(data, {"agent.turns": "turns"}, handled_prefixes=("llm.",)) == []

    def test_a_prefix_does_not_shield_a_sibling(self):
        # "llm." must not accidentally cover "llmx.port".
        dropped = dropped_yaml_keys({"llmx": {"port": 1}}, {}, handled_prefixes=("llm.",))
        assert dropped == ["llmx.port"]


class TestUnknownFieldNames:
    def test_valid_fields_pass(self):
        assert unknown_field_names({"a.b": "b"}, {"b", "c"}) == []

    def test_a_typo_is_reported_once_per_field(self):
        mapping = {"a.b": "b_typo", "c.d": "b_typo"}
        assert unknown_field_names(mapping, {"b"}) == ["b_typo"]
