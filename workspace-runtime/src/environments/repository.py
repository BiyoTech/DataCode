"""Read-only view of the environment registry.

``environments`` and ``environment_events`` are CONTROL-SERVICE's tables: it
creates them in the migrate step and owns every write. This service reads one
row of them, on the plan / apply gate, and that read is deliberately direct —
it is on the hot path, tolerates a row that is a moment stale, and cannot
corrupt anything.

Writes are a different matter and are NOT here. The two facts this runtime
authors (the watermark a successful apply leaves, the audit row a merge
produces) are sent to the control plane through
:mod:`src.environments.registry_client`. Two services writing one table is the
shape of every ownership bug this platform has already paid for: the
``create_all`` race on boot, and a reclaim routine that was fixed on one side
while its verbatim copy here kept the timestamp bug.

The write methods that used to sit in this module were verbatim copies of
control's repository with no caller at all — kept alive by inertia, which is
exactly how such a copy drifts. Removing them turns "the runtime does not write
platform metadata" from a comment into something the code cannot express;
``tests/test_control_owned_tables_readonly.py`` keeps it that way.

Authorization is not performed here: the gateway is the platform's single
authorization center. Tenant scoping IS enforced, so an environment belonging
to another tenant is reported as missing.
"""

from __future__ import annotations

import typing as t

from sqlalchemy import select

from src.environments.models import EnvironmentModel
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)


class EnvironmentRepository:
    """Read-only database access to the control plane's environment registry."""

    async def get(
        self, environment_id: str, tenant_id: str = ""
    ) -> t.Optional[EnvironmentModel]:
        """Get an environment by registry key, optionally scoped to a tenant."""
        conditions = [EnvironmentModel.id == environment_id]
        if tenant_id:
            conditions.append(EnvironmentModel.tenant_id == tenant_id)

        async with get_session() as session:
            result = await session.execute(select(EnvironmentModel).where(*conditions))
            return result.scalar_one_or_none()
