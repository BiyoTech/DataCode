"""Registry of in-flight chat runs — used for cancellation."""

import typing as t

active_chat_runs: dict[str, dict[str, t.Any]] = {}
