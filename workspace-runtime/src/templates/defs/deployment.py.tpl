"""Deployment identity and scheduling gate for workspace {{ workspace_name }}.

PLATFORM-MANAGED FILE. dagster-service regenerates it whenever its content
drifts from the platform template, so local edits are lost. Put your own code in
the sibling packages (``defs/assets``, ``defs/jobs``, ``defs/schedules``, ...).

What this module is for
-----------------------
Every environment of a workspace runs its own Dagster deployment, and all of
them load THE SAME code from their own Git branch. Nothing in the code itself can
therefore tell production apart from a feature branch — that distinction is
injected by dagster-service as environment variables when it launches the code
location, and this module is where the code reads it.

The consequence that matters is scheduling: without a gate, a sandbox
environment's schedule would be picked up by the daemon and drive production data
on every tick. Declare schedules and sensors with the statuses exported here::

    import dagster as dg

    from defs.deployment import DEFAULT_SCHEDULE_STATUS

    daily = dg.ScheduleDefinition(
        name="daily_refresh",
        job=my_job,
        cron_schedule="0 2 * * *",
        execution_timezone="UTC",
        default_status=DEFAULT_SCHEDULE_STATUS,
    )

Enabling schedules for a non-production environment is a platform decision, not a
code one: the environment registry sets it on the environment and dagster-service
injects it here. Hard-coding ``RUNNING`` bypasses that decision.

Why this lives under ``defs/`` and not in ``definitions.py``: ``definitions.py``
imports the ``defs`` package, so a module under ``defs`` cannot import back from
it without a circular import.
"""

import os

import dagster as dg

# ── Deployment identity (injected by dagster-service) ──────────────────
# Empty values mean the code was loaded outside a platform deployment (a local
# `dagster dev`, a unit test); the gate then falls back to the safest behavior.
ENVIRONMENT = os.getenv("DATACODE_ENVIRONMENT", "")
DEPLOYMENT_NAME = os.getenv("DATACODE_DEPLOYMENT", "")
DEPLOYMENT_TYPE = os.getenv("DATACODE_DEPLOYMENT_TYPE", "sandbox")
WORKSPACE_ID = os.getenv("DATACODE_WORKSPACE_ID", "")
TENANT_ID = os.getenv("DATACODE_TENANT_ID", "")

# Whether this deployment is the workspace's production line.
IS_PRODUCTION = DEPLOYMENT_TYPE == "production"

# Whether the daemon may run this deployment's schedules and sensors. Granted to
# production automatically; to any other environment only explicitly.
SCHEDULES_ENABLED = os.getenv("DATACODE_SCHEDULES_ENABLED", "") == "1"

# ── Scheduling gate ────────────────────────────────────────────────────
DEFAULT_SCHEDULE_STATUS = (
    dg.DefaultScheduleStatus.RUNNING
    if SCHEDULES_ENABLED
    else dg.DefaultScheduleStatus.STOPPED
)

DEFAULT_SENSOR_STATUS = (
    dg.DefaultSensorStatus.RUNNING
    if SCHEDULES_ENABLED
    else dg.DefaultSensorStatus.STOPPED
)


def run_tags() -> dict[str, str]:
    """Platform tags identifying this deployment, for runs launched from code.

    Runs launched through the platform API already carry these; use this helper
    when your own code launches a run (a sensor's ``RunRequest``) so it stays
    attributable to the environment that produced it.
    """
    tags = {
        "datacode/environment": ENVIRONMENT,
        "datacode/workspace_id": WORKSPACE_ID,
        "datacode/deployment": DEPLOYMENT_NAME,
    }
    return {key: value for key, value in tags.items() if value}
