#!/bin/sh
set -e

run_migrations() {
    uv run alembic upgrade head
}

# `migrate` will be invoked only by a dedicated one-shot ECS migrate
# task -- cd-infra#67 adds that task, an entryPoint override on the
# long-running service task so it skips this script, and the
# cd-server-deploy.yml step that runs `migrate` and waits for exit 0
# before redeploying. That's what lets the ECS service move to a
# rolling (surge) deployment without two briefly-overlapping app tasks
# racing `alembic upgrade head` against cd_customers. Mirrors cd-etl's
# own entrypoint split. Until then this branch is unused in practice.
if [ "$1" = "migrate" ]; then
    run_migrations
    exit 0
fi

# No `migrate` arg: local dev / CI, and -- until cd-infra#67 lands --
# production too. `docker compose up cd-server` and `make test-server`
# get a migrated cd_customers with no separate step; the current prod
# service task (no entryPoint override yet) likewise migrates on boot,
# which is safe only because the service runs one task at a time.
#
# CD_SERVER_MIGRATE_TASK=1 opts a task OUT of this: cd-infra sets it on
# the long-running service task when it adds the dedicated one-shot
# `entrypoint.sh migrate` task (cd-infra#67), so migrations then have
# exactly one owner and the service can move to a surge deployment. It
# defaults to running migrations, so this stays safe to ship and
# release before that infra exists (unset -> today's behavior), and it
# doubles as a backstop: a service task that reaches this script with a
# dropped/misconfigured entryPoint override still skips the migrate
# because the env var is set alongside that override.
if [ "${CD_SERVER_MIGRATE_TASK:-0}" = "1" ]; then
    echo "entrypoint: CD_SERVER_MIGRATE_TASK=1 -- migrations are owned by the one-shot 'migrate' task; skipping the on-boot run." >&2
else
    run_migrations
fi

exec "$@"
