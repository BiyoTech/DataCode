"""Example sensor for workspace {{ workspace_name }}.

Sensors poll for external conditions and can launch runs when they are met.
This scaffold always skips; replace the body with your trigger logic (e.g. a
new file in object storage, an upstream table refresh, a webhook cursor).

Auto-discovered by ``definitions.py``: the top-level sensor object is picked up
directly.
"""

import dagster as dg

from defs.jobs.example_job import example_job


@dg.sensor(job=example_job, default_status=dg.DefaultSensorStatus.STOPPED)
def example_sensor(context: dg.SensorEvaluationContext) -> dg.SkipReason:
    """Skip by default; implement a real trigger condition here."""
    context.log.info("example_sensor evaluated (scaffold: always skips)")
    return dg.SkipReason("Scaffold sensor: no trigger condition implemented yet.")
