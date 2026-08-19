#!/bin/bash
set -e

uv run airflow db migrate
uv run alembic upgrade head

# `create` no-ops if the user already exists; the unconditional
# `reset-password` after it guarantees the admin password always matches
# the current AIRFLOW_ADMIN_PASSWORD, even across restarts against an
# already-provisioned metadata DB. No in-script default/fallback for the
# password -- missing/empty is a hard failure, not a silently generated
# or weak one.
#
# --password is a plaintext CLI arg (briefly visible via `ps`/
# `/proc/<pid>/cmdline` to anyone with container exec access) --
# apache-airflow-providers-fab's `users create`/`reset-password` has no
# stdin/secure-input option. Not a new exposure in practice:
# AIRFLOW_ADMIN_PASSWORD is already readable via this same container's
# own environment (`docker exec ... env`) for its whole lifetime, to the
# same access level this would require.
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
    # here instead -- bash specifically (not dash/#!/bin/sh), since
    # `wait -n` below needs it.
    uv run airflow scheduler &
    scheduler_pid=$!
    uv run airflow dag-processor &
    dag_processor_pid=$!
    uv run airflow triggerer &
    triggerer_pid=$!
    uv run airflow api-server &
    api_server_pid=$!

    # Forward `docker stop`'s SIGTERM to all four -- without this, only a
    # directly exec'd process would receive it, and the rest would be
    # orphaned and SIGKILL'd ungracefully after the grace period.
    trap 'kill $scheduler_pid $dag_processor_pid $triggerer_pid $api_server_pid 2>/dev/null' TERM INT

    # `set -e` alone doesn't apply to backgrounded commands -- without an
    # explicit check, one of these crashing right after `&` would go
    # unnoticed (the container would keep reporting healthy via
    # api-server alone). `wait -n` returns as soon as any one of the four
    # exits, whether from a crash or the trap above killing them during a
    # graceful shutdown; either way, letting the script exit at that
    # point (via `set -e`, since a crash's exit code is non-zero) tears
    # the whole container down rather than continuing to run degraded.
    wait -n $scheduler_pid $dag_processor_pid $triggerer_pid $api_server_pid
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
