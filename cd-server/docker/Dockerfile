FROM python:3.12-slim AS base

RUN pip install --no-cache-dir uv

WORKDIR /app

# Build context is the repo root (not cd-server/ alone) specifically so
# cd-lib -- a sibling directory -- is reachable at all; Docker can't COPY
# from outside its own context. Placed at /cd-lib, one level above /app,
# so pyproject.toml's `../cd-lib` path source (relative to the pyproject.toml
# it's declared in) resolves the same way here as it does on a real
# checkout, where cd-server/pyproject.toml's "../cd-lib" means repo-root/cd-lib.
COPY cd-lib/pyproject.toml /cd-lib/pyproject.toml
COPY cd-lib/cd/ /cd-lib/cd/

COPY cd-server/pyproject.toml cd-server/uv.lock ./
RUN uv sync --locked --no-dev

COPY cd-server/src/ ./src/

# UV_NO_SYNC is what makes --no-dev above actually stick: uv run performs
# its own implicit sync on every invocation regardless of what a prior
# `uv sync` used, which would otherwise silently reinstall pytest and the
# rest of the dev dependency group into a running container (including
# production). With this set, every stage's venv stays exactly what its
# own build-time `uv sync` produced.
#
# PYTHONPATH is needed for the same reason cd-etl's Dockerfile needs it:
# uvicorn imports `cd.server.app` as a plain module, and nothing else on
# the default path puts `src/` (where the `cd` package lives) on
# sys.path -- pytest doesn't need this (pyproject.toml's own
# `pythonpath = ["src"]` handles it for the test run), so this only
# matters for actually running the app.
ENV PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1 \
    PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "cd.server.app:app", "--host", "0.0.0.0", "--port", "8000"]

# What actually ships to GHCR -- no test dependencies, no tests/, since
# `base`'s own `uv sync --locked --no-dev` never installed pytest.
FROM base AS production

# What local dev and CI run the test suite through. A second, full
# `uv sync --locked` (no --no-dev) actually installs pytest on top of
# base's venv before tests/ is copied in.
FROM base AS development
RUN uv sync --locked
COPY cd-server/tests/ ./tests/
