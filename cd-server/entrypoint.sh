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
# pytest tests/` get a migrated cd_customers with no separate step. This
# unconditional run never happens in production (see above).
run_migrations

exec "$@"
