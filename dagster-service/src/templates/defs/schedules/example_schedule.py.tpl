"""Example daily schedule for workspace {{ workspace_name }}.

Runs ``example_job`` once a day. The cron expression is interpreted in UTC
(``execution_timezone="UTC"``) to keep scheduling consistent with the backend's
UTC-only policy; presentation-layer timezone conversion happens in the UI.

``default_status`` comes from ``defs.deployment``: this schedule runs only in a
deployment the platform allows to be scheduled. Every environment loads this same
code from its own branch, so a hard-coded ``RUNNING`` would let a sandbox drive
production data on every tick.

Auto-discovered by ``definitions.py``: the top-level schedule object is picked
up directly.
"""

import dagster as dg

from defs.deployment import DEFAULT_SCHEDULE_STATUS
from defs.jobs.example_job import example_job

# 00:00 UTC every day. Adjust the cron string to your cadence.
daily_schedule = dg.ScheduleDefinition(
    name="daily_example_job",
    job=example_job,
    cron_schedule="0 0 * * *",
    execution_timezone="UTC",
    default_status=DEFAULT_SCHEDULE_STATUS,
)
