"""Example sensor for workspace {{ workspace_name }}.

Sensors poll for external conditions and can launch runs when they are met.
This scaffold always skips; replace the body with your trigger logic (e.g. a
new file in object storage, an upstream table refresh, a webhook cursor).

``default_status`` comes from ``defs.deployment`` for the same reason schedules
gate on it: a sensor in a branch deployment would otherwise launch runs against
production on its own.

Auto-discovered by ``definitions.py``: the top-level sensor object is picked up
directly.
"""

import dagster as dg

from defs.deployment import DEFAULT_SENSOR_STATUS
from defs.jobs.example_job import example_job


@dg.sensor(job=example_job, default_status=DEFAULT_SENSOR_STATUS)
def example_sensor(context: dg.SensorEvaluationContext) -> dg.SkipReason:
    """Skip by default; implement a real trigger condition here."""
    context.log.info("example_sensor evaluated (scaffold: always skips)")
    return dg.SkipReason("Scaffold sensor: no trigger condition implemented yet.")
