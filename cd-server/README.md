# CD-Server

FastAPI + GraphQL (Strawberry) backend for `cd-webapp`, the React app that
lets users research representatives, legislation, and voting records.

## What it does

`src/cd/server/app.py` mounts a GraphQL endpoint at `/graphql` (built from
the schema in `src/cd/server/schema.py`) plus two plain REST endpoints:
`GET /health` (used by CI and, eventually, an ECS/ALB target group) and
`GET /version`.

The schema (`src/cd/server/schema.py`) currently exposes:

```graphql
{
  version
  getSenators(state: "CA") { firstName lastName party }
  getRepresentatives(state: "CA", district: 12) { firstName lastName role }
}
```

`GET /version` returns the same value as a plain REST call --
`{"version": "..."}`, same shape as `cd-api`'s own `GET /version` --
for a quick `curl` check without a GraphQL client. Both read from the
same `VERSION`-file-driven source of truth (`"dev"` when no `VERSION`
file is present, true for every local/test run since it's only ever
written into the image at release time) via `../cd-lib`'s shared
`read_version()` -- see `cd-lib/README.md` for why that's the first
piece of code shared across `cd-platform`'s Python services, and the
`cd`-namespace-package detail that makes it work.

`getSenators`/`getRepresentatives` are cd-server's first real
server-to-server calls to `cd-api` -- `src/cd/server/clients.py`
provides two interchangeable implementations picked by
`settings.ENVIRONMENT`: `HttpApiClient` (plain HTTP, for local dev) and
`LambdaApiClient` (direct `boto3` invoke of the real deployed function,
bypassing API Gateway entirely -- no network hop, no `X-Api-Key` needed,
since cd-api's own code never checks that header). `LambdaApiClient`
builds a synthetic API-Gateway-shaped event and calls cd-api's actual
Mangum handler with it, so routing/validation/error-formatting all get
exercised exactly as they would over real HTTP rather than reaching
around cd-api's HTTP layer to call its internal functions directly.

Down the line, `cd-server` will also get its own Postgres database,
issue/manage API keys and billing for authenticated users, and resolve
a free-text address to a state/district (e.g. via the Census Bureau's
geocoding API) -- a separate integration from cd-api, not built yet.

### Calling cd-api locally

`HttpApiClient`'s default target is `http://host.docker.internal:8000`
(overridable via `CD_API_BASE_URL`) -- reachable from cd-server's own
container via the `extra_hosts` entry in `../docker-compose.yml` (Linux
doesn't resolve `host.docker.internal` by default the way Docker
Desktop does). Start `cd-api` yourself first, bound to all interfaces
(uvicorn's own default, `127.0.0.1`, isn't reachable from inside a
container):

```bash
cd ../cd-api && uv run uvicorn cd.api.app:app --app-dir src --host 0.0.0.0 --port 8000
```

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
`cd-server/src` and `cd-lib/src` are both bind-mounted, so edits to
either show up without rebuilding. `tests/` is bind-mounted too, so
`make test-server` picks up local test edits without a rebuild.

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
