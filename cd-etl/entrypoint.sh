#!/bin/sh
set -e

uv run airflow db migrate
uv run alembic upgrade head

if [ "$#" -eq 0 ]; then
    # FabAuthManager's tables were already migrated above (FABDBManager
    # auto-registers once AIRFLOW__CORE__AUTH_MANAGER is set) -- no
    # separate migration step needed here.
    #
    # `create` no-ops if the user already exists; the unconditional
    # `reset-password` after it guarantees the admin password always
    # matches the current AIRFLOW_ADMIN_PASSWORD, even across restarts
    # against an already-provisioned metadata DB. No in-script
    # default/fallback for the password -- missing/empty is a hard
    # failure, not a silently generated or weak one.
    : "${AIRFLOW_ADMIN_PASSWORD:?AIRFLOW_ADMIN_PASSWORD must be set}"
    uv run airflow users create \
        --username admin \
        --firstname Admin \
        --lastname User \
        --role Admin \
        --email admin@cd-platform.local \
        --password "$AIRFLOW_ADMIN_PASSWORD"
    uv run airflow users reset-password \
        --username admin \
        --password "$AIRFLOW_ADMIN_PASSWORD"

    # `airflow standalone` can't be used with FabAuthManager -- Airflow
    # core's StandaloneCommand.calculate_env() unconditionally forces
    # AIRFLOW__CORE__AUTH_MANAGER back to SimpleAuthManager before
    # launching its subprocesses, no override flag exists. It otherwise
    # just launches these same 4 processes, so they're started directly
    # here instead. Production's decomposed ECS tasks each pass their own
    # explicit command and hit the `else` branch below instead, never
    # reaching this code path.
    uv run airflow scheduler &
    uv run airflow dag-processor &
    uv run airflow triggerer &
    exec uv run airflow api-server
else
    exec "$@"
fi
