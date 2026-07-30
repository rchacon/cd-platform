FROM python:3.12-slim AS base

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# UV_NO_SYNC is what makes --no-dev above actually stick: uv run performs
# its own implicit sync on every invocation regardless of what a prior
# `uv sync` used, which would otherwise silently reinstall pytest and the
# rest of the dev dependency group into a running container (including
# production) the moment entrypoint.sh's `uv run airflow db migrate` runs.
# With this set, every stage's venv stays exactly what its own build-time
# `uv sync` produced.
ENV AIRFLOW_HOME=/app/.airflow \
    AIRFLOW__CORE__DAGS_FOLDER=/app/src \
    AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1

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
COPY tests/ ./tests/
