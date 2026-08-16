# CD-Server

FastAPI + GraphQL (Strawberry) backend for `cd-webapp`, the React app that
lets users research representatives, legislation, and voting records.

## What it does

`src/cd/server/app.py` mounts a GraphQL endpoint at `/graphql` (built from
the schema in `src/cd/server/schema.py`) plus two plain REST endpoints:
`GET /health` (used by CI and, eventually, an ECS/ALB target group) and
`GET /version`.

Currently the schema exposes a single query:

```graphql
{
  version
}
```

`GET /version` returns the same value as a plain REST call --
`{"version": "..."}`, same shape as `cd-api`'s own `GET /version` --
for a quick `curl` check without a GraphQL client. Both read from the
same `VERSION`-file-driven source of truth (`"dev"` when no `VERSION`
file is present, true for every local/test run since it's only ever
written into the image at release time).

Down the line, `cd-server` will get its own Postgres database, issue and
manage API keys, handle billing for authenticated users, and make
authenticated server-to-server calls to `cd-api` on behalf of
`cd-webapp`'s anonymous users -- none of that is built yet.

## Prerequisites

- Docker -- the only local dependency. No `uv`/Python install needed;
  everything runs inside the `cd-server` container defined in
  `../docker-compose.yml`, built from `docker/Dockerfile` -- the same
  image (also pushed to GHCR on a `cd-server-v*` tag, see below) local dev
  and deployment both run.

## Setup

From the repo root:

```bash
make start-server
```

Open `http://localhost:8000/graphql` for the GraphiQL IDE,
`http://localhost:8000/health` for the health check, or `curl
http://localhost:8000/version` for a quick version check.
`cd-server/src` is bind-mounted, so edits show up without rebuilding.

## Testing

```bash
make test-server
```

`docker/Dockerfile` is multi-stage: `production` (what actually ships) has
no test dependencies at all, while `development` (what `make
start-server`/`test-server` build) additionally installs `pytest` and
copies `tests/` in -- so this also doesn't need `uv`/Python on the host.

## Releasing

Pushing a tag matching `cd-server-v*` (e.g. `cd-server-v1.0.0`) triggers
`.github/workflows/cd-server-deploy.yml`, which builds this same
`docker/Dockerfile`'s `production` target and pushes it to GHCR as
`ghcr.io/<owner>/cd-server`. The `cd-server-v` prefix is dropped from the
pushed version tag -- tag `cd-server-v1.0.0` produces image tags `1.0.0`
and `latest`, not `cd-server-v1.0.0`. The workflow's first step
(`../scripts/check-tag-version.sh`) hard-fails the deploy if the tag's
version doesn't match `pyproject.toml`'s own `version`; an optional local
`pre-push` git hook runs the same check before the tag is even pushed
(`git config core.hooksPath .githooks`, see the root `CLAUDE.md`).

Deployed as a container (not a Lambda zip like `cd-api`) since `cd-server`
is expected to hold long-lived state and connections once it gets its own
database -- the intended production target is an ECS service backed by
EC2, provisioned in `cd-infra`.
