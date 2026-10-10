"""Step names of the recorded workspace lifecycle operations.

A step names an effect that has been ACHIEVED, recorded right after it. The
resumer reads the last one to decide between finishing and undoing; see
:mod:`src.operations.resumer` for the policy per kind.
"""

# ── create ────────────────────────────────────────────────────────────────
BEGUN = "begun"
# The remote was resolved; the workspace row is about to be written. Recorded
# BEFORE the insert so a crash between insert and the next record is still
# attributed to this operation (the resumer checks row ownership by time).
WRITING_ROW = "writing_row"
ROW_WRITTEN = "row_written"
# Production environment registered and the owner's copy materialized.
OPENED = "opened"
SCAFFOLDED = "scaffolded"
PUSHED = "pushed"

# ── delete ────────────────────────────────────────────────────────────────
LEASES_RELEASED = "leases_released"
RESOURCES_RELEASED = "resources_released"
REPLICAS_PURGED = "replicas_purged"
ROWS_DELETED = "rows_deleted"
