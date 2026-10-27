"""Guards against silently-dropped configuration keys.

The failure mode these helpers catch has already happened twice in this
platform, and both times it shipped unnoticed:

* a key present in ``conf/<service>.yaml`` but absent from the service's
  ``_flatten_yaml`` mapping is dropped on the floor — ``task.backend`` sat in
  the YAML while every deployment ran with the built-in default and refused
  every sandboxed task;
* a mapping VALUE that is not a field of the Settings model is dropped just as
  silently, because pydantic ignores unknown init kwargs.

The contract these enforce is one sentence: every key a shipped YAML file
declares must land on the Settings object — either through the scalar mapping
or through one of the explicitly documented nested blocks.
"""

from __future__ import annotations

import typing as t


def yaml_leaf_keys(data: t.Any, prefix: str = "") -> frozenset[str]:
    """Dot-paths of every leaf (scalar or list) in a nested YAML mapping.

    Lists count as leaves: they are consumed whole (``llm.models``) rather than
    path-by-path, so a list's elements are not walked.
    """
    keys: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            keys |= yaml_leaf_keys(value, f"{prefix}{key}.")
    else:
        keys.add(prefix.rstrip("."))
    return frozenset(keys)


def dropped_yaml_keys(
    data: t.Any,
    mapping: t.Mapping[str, str],
    *,
    handled_prefixes: t.Sequence[str] = (),
) -> t.List[str]:
    """YAML leaf keys that reach neither the mapping nor a nested block.

    ``handled_prefixes`` names the blocks a service flattens procedurally (for
    example agent-service's ``llm.`` multi-model config). Everything else must
    appear in the scalar mapping, or the key is silently ignored.
    """
    return sorted(
        key
        for key in yaml_leaf_keys(data)
        if key not in mapping
        and not any(key.startswith(prefix) for prefix in handled_prefixes)
    )


def unknown_field_names(
    mapping: t.Mapping[str, str], model_fields: t.Container[str]
) -> t.List[str]:
    """Mapping values that are not fields of the Settings model.

    ``flatten_yaml_mapping`` feeds values into ``Settings(**kwargs)``; a value
    naming no field is ignored by pydantic, which turns a mapping typo into a
    silent default.
    """
    return sorted(
        dict.fromkeys(
            field for field in mapping.values() if field not in model_fields
        )
    )
