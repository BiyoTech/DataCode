"""HTTP-layer building blocks shared by the Python services.

Only things whose BEHAVIOUR must be identical across services belong here —
authorization input and log correlation. Response envelopes deliberately stay
per service: their key names are a frontend contract, not an internal detail.
"""

from datacode_common.api.caller import CallerAuth, CallerContext
from datacode_common.api.log_context import LogContextMiddleware

__all__ = [
    "CallerAuth",
    "CallerContext",
    "LogContextMiddleware",
]
