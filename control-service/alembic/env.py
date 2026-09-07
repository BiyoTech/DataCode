"""Alembic migration environment for control-service.

The schema is SHARED with workspace-runtime (one database, two owners of
disjoint table sets); control-service is the chain's custodian since P5.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.config import settings
from src.workspace.database import _build_database_url
from src.workspace.models import Base

# Importing the model modules is what populates ``Base.metadata`` used for
# autogenerate. The environment registry lives outside src.workspace, so it has
# to be imported explicitly.
from src.environments import models as environment_models  # noqa: F401,E402
from src.quota import models as quota_models  # noqa: F401,E402
from src.records import models as record_models  # noqa: F401,E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    url = _build_database_url()
    # Alembic migrates over a SYNC connection while the service itself runs
    # async: swap the async drivers for their sync counterparts. MySQL maps to
    # pymysql (the driver aiomysql is built on) because the default ``mysql://``
    # scheme would require the mysqlclient C extension.
    return (
        url.replace("+aiosqlite", "")
        .replace("+asyncpg", "+psycopg2")
        .replace("+aiomysql", "+pymysql")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
