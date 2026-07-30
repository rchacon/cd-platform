#!/bin/sh
set -e

uv run airflow db migrate
uv run alembic upgrade head

if [ "$#" -eq 0 ]; then
    exec uv run airflow standalone
else
    exec "$@"
fi
