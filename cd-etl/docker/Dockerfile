FROM python:3.12-slim AS base

RUN pip install --no-cache-dir uv

WORKDIR /app

# Build context is the repo root (not cd-etl/ alone) so cd-lib -- a
# sibling directory -- is reachable at all; Docker can't COPY from
# outside its own context. Placed at /cd-lib, one level above /app, so
# cd-etl/pyproject.toml's `../cd-lib` path source resolves the same way
# here as on a real checkout. Same setup as cd-server/docker/Dockerfile.
COPY cd-lib/pyproject.toml /cd-lib/pyproject.toml
COPY cd-lib/src/ /cd-lib/src/

COPY cd-etl/pyproject.toml cd-etl/uv.lock ./
RUN uv sync --locked --no-dev

COPY cd-etl/src/ ./src/
COPY cd-etl/alembic.ini ./
COPY cd-etl/migrations/ ./migrations/
COPY cd-etl/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# UV_NO_SYNC is what makes --no-dev above actually stick: uv run performs
# its own implicit sync on every invocation regardless of what a prior
# `uv sync` used, which would otherwise silently reinstall pytest and the
# rest of the dev dependency group into a running container (including
# production) the moment entrypoint.sh's `uv run airflow db migrate` runs.
# With this set, every stage's venv stays exactly what its own build-time
# `uv sync` produced.
#
# PYTHONPATH is needed because of how Airflow parses DAG files: it adds
# only that specific file's own directory to sys.path, not the configured
# DAGS_FOLDER root -- fine when every module lived flat in src/, but
# src/cd/etl/dags/*.py's own absolute imports (e.g. `from cd.etl import
# bills_common`) need src/ itself on the path to resolve the `cd` package.
# pytest doesn't need this (pyproject.toml's own `pythonpath = ["src"]`
# handles it for the test run), so this only matters for the app itself.
#
# AUTH_MANAGER picks FabAuthManager over Airflow 3's zero-config default
# (SimpleAuthManager), which auto-generates and logs a random admin
# password on every fresh AIRFLOW_HOME -- see entrypoint.sh for the
# explicit admin account this requires instead. FAB's own metadata tables
# ride along with `airflow db migrate` automatically (FABDBManager
# auto-registers once this is set), no separate migration step needed.
ENV AIRFLOW_HOME=/app/.airflow \
    AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager \
    AIRFLOW__CORE__DAGS_FOLDER=/app/src \
    AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1 \
    PYTHONPATH=/app/src

# Runs as root -- a dedicated non-root user needs a privilege-drop
# entrypoint (gosu/su-exec) to fix ownership of Docker-created named
# volumes (which mount root-owned regardless of what the image sets),
# not worth the extra moving part for a single-maintainer, non-multi-
# tenant container.
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]

# What actually ships to GHCR -- no test dependencies, no tests/, since
# `base`'s own `uv sync --locked --no-dev` never installed pytest.
FROM base AS production

# What local dev and CI run the test suite through. A second, full
# `uv sync --locked` (no --no-dev) actually installs pytest on top of
# base's venv before tests/ is copied in.
FROM base AS development
RUN uv sync --locked
COPY cd-etl/tests/ ./tests/
