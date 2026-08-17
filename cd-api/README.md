# CD-API

REST API that `cd-lookup` (the WordPress plugin) consumes for representative
lookups, replacing its current GovTrack HTML scrape. Exposes the
`current_members` view (defined in
`../cd-etl/migrations/versions/0001_initial_schema.py`) over HTTP as JSON.

## What it does

`src/cd/api/app.py` defines a FastAPI app with one route:

```
GET /members?state=GA&district=5
-> { "senators": [...], "representatives": [...] }

GET /members?state=GA
-> { "senators": [...], "representatives": [] }
```

`district` is optional -- senators represent the whole state (every district
in it), so omitting `district` returns senators only; a representative is
only included when `district` is given and matches.

Each person has `bioguide_id` (Congress.gov's stable identifier for that
member -- the primary key `members.bioguide_id` is keyed on), `first_name`,
`middle_name`, `last_name`, `nickname`, `suffix` (the individual name
parts, passed through as-is -- the API does not derive a combined display
name; that's left to the client), `role` (whatever Congress.gov's
`member_type` records for that seat -- `"Senator"`, `"Representative"`,
`"Delegate"` for a non-voting territory seat (DC, American Samoa, Guam,
Northern Mariana Islands, or the US Virgin Islands), or `"Resident
Commissioner"` specifically for Puerto Rico's non-voting seat), `party`,
`phone`, `website`, `photo_url`, `district` (`member_terms.district`'s own
`null`/`0`/`1+` convention passed straight through -- `null` for a
Senator, `0` for an at-large House seat, `1+` for a numbered one). An
unknown state returns `404`. A `district`
that doesn't exist for that state (validated against real House
apportionment via `src/cd/api/apportionment.py`'s
`max_valid_district`/`is_valid_district`, built on `../cd-lib`'s shared
`SEATS_PER_STATE` table -- see `cd-lib/README.md`) also returns `404` -- e.g.
`district=99` for a 14-district state. A `district` that *does* exist but
currently has no representative (a genuine vacancy) still returns `200` with
an empty `representatives` list, distinct from the `404` above.

Errors follow [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) ("Problem
Details for HTTP APIs") -- `Content-Type: application/problem+json`, body
shaped `{"type", "title", "status", "detail", ...}`. Handled uniformly for
unknown states (`404`), request validation failures like a malformed `state`
(`422`, with a JSON-serialized `errors` list), and any unhandled server error
(`500`), via `src/cd/api/problem.py`'s `problem_response` and the exception
handlers registered in `src/cd/api/app.py`.

`src/cd/api/db.py` queries `current_members` directly with `psycopg2` -- no
connection pooling yet, that's an open question for AWS deployment (see
issue #4). `src/cd/api/transform.py` holds the pure row -> JSON-shape
functions. `../cd-lib/src/cd/lib/models.py` holds the Pydantic models
documenting those same request/response shapes (`response_model=`/
`responses=` on each route in `src/cd/api/app.py`), so the OpenAPI spec
exported on release (see Releasing below) actually reflects what the API
returns, including error bodies -- shared with `cd-server` via `cd-lib`
(see `../cd-lib/README.md`) rather than living only in `cd-api`.

`handler = Mangum(app)` in `app.py` is what an AWS Lambda config points to
(as the dotted path `cd.api.app.handler` -- see Releasing below for why the
`cd.api` package structure has to be preserved, not flattened, in the
deploy zip); it's untouched for local development.

`GET /version` returns `{"version": ...}`, read from a `VERSION` file next
to `app.py` -- only present in a deployed Lambda zip (written by
`cd-api-deploy.yml`, see Releasing below), so it always reads `"dev"`
locally.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Docker (for the Postgres container defined in `../docker-compose.yml`)

## Setup

1. Start Postgres and apply the schema (owned by `cd-etl`, see
   `../cd-etl/README.md`):

   ```bash
   cd .. && docker compose up -d postgres
   cd cd-etl && uv run alembic upgrade head && cd ../cd-api
   ```

2. Install dependencies:

   ```bash
   uv sync
   ```

3. Run it:

   ```bash
   uv run uvicorn cd.api.app:app --reload --app-dir src
   ```

   Then, e.g.:

   ```bash
   curl 'http://localhost:8000/members?state=GA&district=5'
   ```

## Testing

```bash
uv run pytest tests/
```

`tests/test_transform.py` is pure unit tests with no dependencies.
`tests/test_app.py` seeds real rows and exercises the endpoint end to end
against Postgres, and skips itself if `docker compose up -d postgres` hasn't
been run.

Tests target a dedicated `congressional_app_test` database (`cd-platform#16`),
not the real dev database -- `tests/conftest.py` sets this before
`cd.api.db`/`cd.api.app` are first imported, so both the fixture's own
seeding and the app code under test (via `TestClient`) consistently hit the
same isolated database. That database's schema is owned by `cd-etl` (see
`../cd-etl/README.md`), so run
`make test-etl` from the repo root at least once first -- otherwise these
tests fail with "relation does not exist" rather than skipping, since the
database itself already exists (just without the schema yet).

## Releasing

Pushing a tag matching `cd-api-v*` (e.g. `cd-api-v0.1.0`) triggers
`.github/workflows/cd-api-deploy.yml`, which builds a Lambda zip package
(production dependencies via `uv export`, cross-installed for Lambda's
x86_64/Python 3.12 runtime, at the zip root; `src/cd/` copied in alongside
them, preserving its `cd/api/` structure rather than flattening it -- see
`cd-infra#12`: Lambda's runtime only puts the zip root on `sys.path`, so
`app.py`'s own absolute imports, e.g. `from cd.api.db import ...`, need the
`cd` package to sit directly there) and ships it directly to the
`cd-platform-cd-api` Lambda function via `aws lambda update-function-code`.
The build also writes a `VERSION` file (the tag with its `cd-api-v` prefix
stripped, e.g. `0.1.0`) into `package/cd/api/` (alongside `app.py`, since
`GET /version` reads it via `Path(__file__).parent`) -- the simplest way to
confirm what's actually live behind Lambda's mutable `$LATEST`, without
needing Lambda's own PublishVersion/alias machinery.

`cd-infra`'s Terraform configures the Lambda's `handler` as
`cd.api.app.handler`, matching this workflow's package structure.

Authenticates via GitHub OIDC to a scoped IAM role
(`cd-platform-cd-api-deploy`, provisioned in
`cd-infra`'s `terraform/cd-api/`) -- no static AWS credentials stored in
this repo. Environment variables (DB connection info) are owned by
Terraform, not this workflow. As with `cd-etl`, the workflow's first step
(`../scripts/check-tag-version.sh`) hard-fails the deploy if the tag's
version doesn't match `pyproject.toml`'s own `version`; an optional local
`pre-push` git hook runs the same check before the tag is even pushed
(`git config core.hooksPath .githooks`, see the root `CLAUDE.md`).

The workflow also exports `app.openapi()` to `openapi.json` (reusing the
same installed `package/` dependencies -- no second dependency install,
done right after the package-import sanity check, before the Lambda
deploy) and, once the Lambda deploy succeeds, publishes it via `aws s3 cp`
to a public S3 bucket (`cd-platform-openapi-spec-<account-id>`, provisioned in
`cd-infra`#18, name supplied via the `OPENAPI_SPEC_BUCKET` repo variable) at
a fixed `openapi.json` key, with `Content-Type: application/json` and
`Cache-Control: no-cache`. This exists because API Gateway requires an API
key on every route including `/openapi.json`, so the live spec can't be
fetched client-side by the `cd-website` docs viewer (`cd-website`#1) -- the
public S3 copy sidesteps that. `app.py`'s `FastAPI(title="cd-api",
version=...)` reuses the same `VERSION`-file convention as `GET /version`,
so the exported spec's `info.version` reflects the actual deployed tag
rather than FastAPI's default placeholder.
