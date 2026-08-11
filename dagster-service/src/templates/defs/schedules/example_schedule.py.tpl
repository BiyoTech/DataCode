"""Example daily schedule for workspace {{ workspace_name }}.

Runs ``example_job`` once a day. The cron expression is interpreted in UTC
(``execution_timezone="UTC"``) to keep scheduling consistent with the backend's
UTC-only policy; presentation-layer timezone conversion happens in the UI.

Auto-discovered by ``definitions.py``: the top-level schedule object is picked
up directly.
"""

import dagster as dg

from defs.jobs.example_job import example_job

# 00:00 UTC every day. Adjust the cron string to your cadence.
daily_schedule = dg.ScheduleDefinition(
    name="daily_example_job",
    job=example_job,
    cron_schedule="0 0 * * *",
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.STOPPED,
)
