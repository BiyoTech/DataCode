"""Credential redaction shared by the error archive and the workspace log sink.

Both persist service-generated text to disk inside a user's workspace, so both
must mask the same credential shapes. Keeping one pattern list here means a
newly covered shape protects every writer at once.

The replacement keeps the key visible so a reader knows a secret was present,
but never the value.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

REDACTION_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    # password=secret  /  password: secret
    (re.compile(r"(?i)(password\s*[:=]\s*)\S+", re.IGNORECASE), r"\1***REDACTED***"),
    # token=secret  /  token: secret
    (re.compile(r"(?i)(token\s*[:=]\s*)\S+", re.IGNORECASE), r"\1***REDACTED***"),
    # secret=secret  /  secret: secret
    (re.compile(r"(?i)(secret\s*[:=]\s*)\S+", re.IGNORECASE), r"\1***REDACTED***"),
    # postgresql://user:pass@host  /  mysql://user:pass@host  ...
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)", re.IGNORECASE), r"\1***\2"),
]


def redact_string(text: str) -> str:
    """Apply each redaction pattern to ``text`` in sequence."""
    result = text
    for pattern, replacement in REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value: Any) -> Any:
    """Redact ``value`` recursively, returning a new object.

    Strings are masked, dicts and lists are walked, everything else is
    returned unchanged.
    """
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_dict_in_place(payload: Dict[str, Any]) -> None:
    """Redact every string reachable from ``payload``, mutating it in place.

    Used by writers that must not change the identity of the dict they were
    handed (e.g. a payload already referenced elsewhere).
    """
    for key, value in payload.items():
        payload[key] = redact_value(value)
