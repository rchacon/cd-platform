#!/bin/sh
set -e

# Unconditional -- runs before this script even looks at its arguments,
# so `docker compose run --rm cd-server uv run pytest tests/...` (what
# `make test-server` uses, which supplies its own command) still gets a
# migrated database, same as cd-etl's own entrypoint.sh. No separate
# manual migration step and no "forgot to migrate" failure mode.
uv run alembic upgrade head

exec "$@"
