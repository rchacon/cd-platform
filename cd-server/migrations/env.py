import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool, text

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

    # Serialize concurrent `alembic upgrade` runs against cd_customers: a
    # second runner blocks here until the first releases the lock, then
    # finds itself already at head and no-ops. Belt-and-suspenders with
    # the dedicated one-shot migrate ECS task (see entrypoint.sh) -- it
    # also covers two containers booting together (`docker compose up`, a
    # stray `ecs run-task`, a crash-and-replace overlap).
    #
    # Session-scoped, NOT `pg_advisory_xact_lock`: a migration that opens
    # an `op.get_context().autocommit_block()` (e.g. CREATE INDEX
    # CONCURRENTLY, which the expand/contract discipline this enables
    # tends to need) commits the surrounding transaction mid-run, which
    # would silently drop a transaction-scoped lock for the rest of the
    # upgrade. A session lock is held until explicitly released or the
    # connection closes -- and NullPool closes this connection on `with`
    # exit, including on error, so the lock is always released even
    # though nothing calls pg_advisory_unlock. The key just has to be
    # stable.
    connection.execute(
        text("SELECT pg_advisory_lock(hashtext('cd_customers_alembic'))")
    )

    with context.begin_transaction():
        context.run_migrations()
