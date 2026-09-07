"""Meta and modules endpoints.

Provides system-level information:
- /meta: Version info and running task status
- /modules: List of enabled feature modules

Matches SQLMesh web server's GET /api/meta and GET /api/modules behavior.
"""

import enum
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from src.sqlmesh.task_tracker import get_task_tracker
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["meta"])

logger = get_logger(__name__)

# Current SQLMesh service version
SERVICE_VERSION = "0.2.0"


class Meta(BaseModel):
    """System metadata response."""

    version: str
    has_running_task: bool


class Modules(str, enum.Enum):
    """Available UI feature modules."""

    EDITOR = "editor"
    FILES = "files"
    DATA_CATALOG = "data-catalog"
    PLANS = "plans"
    TESTS = "tests"
    AUDITS = "audits"
    ERRORS = "errors"
    DATA = "data"
    LINEAGE = "lineage"


# Default enabled modules
ENABLED_MODULES: List[Modules] = [
    Modules.EDITOR,
    Modules.FILES,
    Modules.DATA_CATALOG,
    Modules.PLANS,
    Modules.TESTS,
    Modules.ERRORS,
    Modules.DATA,
    Modules.LINEAGE,
]


@router.get("/meta")
async def get_meta() -> Meta:
    """Get system metadata.

    Returns version and whether any background task (plan/apply) is currently running.
    Used by frontend to prevent conflicting operations.
    """
    tracker = get_task_tracker()
    return Meta(
        version=SERVICE_VERSION,
        has_running_task=await tracker.has_running_task(),
    )


@router.get("/modules")
async def get_modules() -> List[Modules]:
    """Get list of enabled feature modules.

    Frontend uses this to decide which UI sections to show.
    """
    return ENABLED_MODULES
