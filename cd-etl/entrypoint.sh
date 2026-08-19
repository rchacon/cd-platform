#!/bin/sh
set -e

uv run airflow db migrate
uv run alembic upgrade head

# `create` no-ops if the user already exists; the unconditional
# `reset-password` after it guarantees the admin password always matches
# the current AIRFLOW_ADMIN_PASSWORD, even across restarts against an
# already-provisioned metadata DB. No in-script default/fallback for the
# password -- missing/empty is a hard failure, not a silently generated
# or weak one.
create_admin_user() {
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
}

if [ "$#" -eq 0 ]; then
    create_admin_user

    # `airflow standalone` can't be used with FabAuthManager -- Airflow
    # core's StandaloneCommand.calculate_env() unconditionally forces
    # AIRFLOW__CORE__AUTH_MANAGER back to SimpleAuthManager before
    # launching its subprocesses, no override flag exists. It otherwise
    # just launches these same 4 processes, so they're started directly
    # here instead.
    uv run airflow scheduler &
    uv run airflow dag-processor &
    uv run airflow triggerer &
    exec uv run airflow api-server
elif [ "$1" = "create-admin-user" ]; then
    # Invoked by cd-infra's one-shot migrate ECS task (which doesn't
    # override entryPoint, so the unconditional migrate steps above still
    # run first) -- the durable admin credential lives outside this repo,
    # so this is the hook cd-infra's own Terraform calls rather than
    # duplicating this logic there. Production's other ECS tasks
    # (scheduler, api-server, ...) each pass their own explicit command
    # and hit the `else` branch below instead, never provisioning the
    # admin account themselves -- only this invocation does.
    create_admin_user
else
    exec "$@"
fi
