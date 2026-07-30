FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV AIRFLOW_HOME=/app/.airflow \
    AIRFLOW__CORE__DAGS_FOLDER=/app/src \
    AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    PATH="/app/.venv/bin:${PATH}"

# Runs as root -- a dedicated non-root user needs a privilege-drop
# entrypoint (gosu/su-exec) to fix ownership of Docker-created named
# volumes (which mount root-owned regardless of what the image sets),
# not worth the extra moving part for a single-maintainer, non-multi-
# tenant container.
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
