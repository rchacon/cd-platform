#!/bin/sh
set -e

# No standalone fallback -- Airflow's 4 components (api-server, scheduler,
# triggerer, dag-processor) each run as their own docker-compose service
# now, not as unsupervised sibling subprocesses of one `airflow standalone`
# process (see root docker-compose.yml). Every invocation must say exactly
# what to run: "migrate" (this project's own one-shot schema-migration
# step, run by the dedicated cd-etl-migrate service) or a real command to
# exec, e.g. `uv run airflow scheduler`.
if [ "$#" -eq 0 ]; then
    echo "Usage: entrypoint.sh <command...>  (e.g. 'migrate', or 'uv run airflow scheduler')" >&2
    exit 1
fi

# Exact single-arg match, not just "$1 = migrate" -- a real command's
# first argument is always "uv" (every command below is either this
# literal sentinel or a `uv run ...` invocation), so this is already a
# non-issue in practice, but requiring the whole argv to be just this one
# word rules out even a hypothetical future collision with some other
# command that happens to take "migrate" as its own first argument.
if [ "$#" -eq 1 ] && [ "$1" = "migrate" ]; then
    uv run airflow db migrate
    uv run alembic upgrade head
    exit 0
fi

exec "$@"
