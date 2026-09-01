#!/bin/sh
set -e

run_migrations() {
    uv run alembic upgrade head
}

# `migrate` is invoked only by the dedicated one-shot ECS migrate task
# (cd-server-deploy.yml runs it and waits for exit 0 before redeploying
# the service). Migrations run here, once, serialized -- and NOT in the
# long-running service task, whose task definition overrides entryPoint
# to skip this script entirely (cd-infra). That's what lets the ECS
# service move to a rolling (surge) deployment without two briefly-
# overlapping app tasks racing `alembic upgrade head` against
# cd_customers. Mirrors cd-etl's own entrypoint split.
if [ "$1" = "migrate" ]; then
    run_migrations
    exit 0
fi

# No `migrate` arg: local dev / CI only. `docker compose up cd-server`
# and `make test-server`'s own `docker compose run --rm cd-server uv run
# pytest tests/` get a migrated cd_customers with no separate step.
#
# Gated on CD_SERVER_ENVIRONMENT (unset -> "local" in both compose
# paths; non-"local" everywhere cd-infra deploys) as a second guard,
# independent of the entryPoint override above: if that override is ever
# dropped or misconfigured, a production service task that falls through
# to here still will not run migrations -- it starts against the
# existing schema, and logs why. env.py's pg_advisory_xact_lock is the
# third guard, for the case migrations somehow run concurrently anyway.
# Same "branch on ENVIRONMENT == local" precedent as
# services/cd_api_service.py and services/users_service.py.
if [ "${CD_SERVER_ENVIRONMENT:-local}" = "local" ]; then
    run_migrations
else
    echo "entrypoint: CD_SERVER_ENVIRONMENT=${CD_SERVER_ENVIRONMENT} is not 'local' -- skipping unconditional migrations (the one-shot 'migrate' task owns them in production). If this is a long-running service task, its entryPoint override is missing." >&2
fi

exec "$@"
