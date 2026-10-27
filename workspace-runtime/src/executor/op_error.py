"""OpError — the structured failure type shared by every sandbox op.

Extracted from ``server.py`` when the read-side metadata ops moved into the
sandbox (P6 close-out): both the server's plan/gate ops and the metadata ops
in ``metadata_ops.py`` raise it, and the dispatch loop turns it into the
protocol's stable ``{"code", "message", ...}`` error envelope.

The wire error shape stays ``{"code", "message", ...}``; extra keys (such as
lint results) ride along inside it. Payload evolution of this kind is what
the protocol freeze permits — the frames and the op set are what must not
move.
"""

from __future__ import annotations


class OpError(Exception):
    """An op failure carrying structured data alongside the stable code."""

    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# Error code shared by every "the caller asked for a model that is not there"
# op. The runtime translates it to its ModelNotFoundError (HTTP 404) instead
# of a generic 500, so the value is part of the de-facto contract.
ERR_MODEL_NOT_FOUND = "model_not_found"
