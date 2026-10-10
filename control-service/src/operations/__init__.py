"""Persisted progress of multi-step workspace lifecycle operations.

Creating and deleting a workspace are sequences of cross-service steps (remote
provisioning, registry rows, lease placement, scaffold, push / lease release,
resource release, replica purge, row deletion). They used to be protected only
by an in-process rollback: a control replica killed mid-sequence (OOM, rolling
update) left a half-built or half-deleted workspace that nothing would ever
revisit.

One row per operation, advanced BEFORE each step's effect is relied upon, is
the minimum that lets another replica tell "finished", "rolled back" and
"interrupted" apart — see :mod:`src.operations.resumer` for what it does with
the third.
"""

from src.operations.models import WorkspaceOperationModel
from src.operations.recorder import (
    KIND_CREATE,
    KIND_DELETE,
    RESUMABLE_STATUSES,
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    OperationHandle,
    OperationRecorder,
    OperationRecordError,
    OperationRow,
    get_operation_recorder,
)

__all__ = [
    "KIND_CREATE",
    "KIND_DELETE",
    "RESUMABLE_STATUSES",
    "STATUS_ABANDONED",
    "STATUS_FAILED",
    "STATUS_ROLLED_BACK",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_SUPERSEDED",
    "OperationHandle",
    "OperationRecordError",
    "OperationRecorder",
    "OperationRow",
    "WorkspaceOperationModel",
    "get_operation_recorder",
]
