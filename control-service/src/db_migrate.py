"""One-shot schema initializer: create the control tables, then migrate.

Why a module rather than an inline ``alembic upgrade head``: the platform's
base tables (workspaces, environments, credentials, events) are owned by the
ORM metadata — alembic's 0001 is an empty baseline on purpose — so ``alembic
upgrade head`` alone creates NOTHING. Before P5's ownership split both
control-service and workspace-runtime ran ``create_all`` against the shared
database, and the tables appeared whichever service won the race; runtime no
longer creates them (see its ``src/workspace/database.py``), so this entry
point is what every deployment's migrate step runs BEFORE any service starts:

* dev:           scripts/dev-lib.sh ``run_db_migrations``
* compose:       the one-shot ``migrate`` service (stack and cell stacks)
* Kubernetes:    the ``control-migrate`` Job (deploy/k8s/base/migrate-job.yaml)

It performs exactly what a control replica does at boot with ``AUTO_MIGRATE=1``
(``create_all`` then ``upgrade head``), is idempotent on every database shape,
and exits. Run it with: ``python -m src.db_migrate``.
"""

import asyncio
import os


async def _main() -> None:
    # init_db reads AUTO_MIGRATE to decide whether to run the alembic chain;
    # the migrate step must always run both halves.
    os.environ["AUTO_MIGRATE"] = "1"

    from src.workspace.database import init_db

    await init_db()


if __name__ == "__main__":
    asyncio.run(_main())
