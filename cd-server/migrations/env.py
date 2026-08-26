import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models -- migrations are raw SQL via op.execute(), same as
# cd-etl's own migrations.
target_metadata = None


def _db_url() -> str:
    # Same PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD convention as
    # cd-etl's migrations/env.py and cd-server/src/cd/server/settings.py's
    # own PG* settings -- deliberately not importing settings.py here so
    # this migration run has no dependency on the app's own asyncpg/
    # pydantic-derived config, only on plain env vars, same as cd-etl.
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    dbname = os.environ.get("PGDATABASE", "cd_customers")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"


connectable = create_engine(_db_url(), poolclass=pool.NullPool)

with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()
