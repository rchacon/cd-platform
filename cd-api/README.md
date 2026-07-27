# CD-API

REST API that `cd-lookup` (the WordPress plugin) consumes for representative
lookups, replacing its current GovTrack HTML scrape. Exposes the
`current_members` view (defined in `../init.sql`) over HTTP as JSON.

## What it does

`src/app.py` defines a FastAPI app with one route:

```
GET /members?state=GA&district=5
-> { "senators": [...], "representatives": [...] }

GET /members?state=GA
-> { "senators": [...], "representatives": [] }
```

`district` is optional -- senators represent the whole state (every district
in it), so omitting `district` returns senators only; a representative is
only included when `district` is given and matches.

Each person has `full_name`, `role` (`"Senator"`/`"Representative"`), `party`,
`phone`, `website`, `photo_url`. An unknown state returns `404`; a known state
with no representative for the given district (bad district number) returns
`200` with an empty `representatives` list.

`src/db.py` queries `current_members` directly with `psycopg2` -- no
connection pooling yet, that's an open question for AWS deployment (see
issue #4). `src/transform.py` holds the pure row -> JSON-shape functions.

`handler = Mangum(app)` in `app.py` is what an AWS Lambda config points to;
it's untouched for local development.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Docker (for the Postgres container defined in `../docker-compose.yml`)

## Setup

1. Start Postgres:

   ```bash
   cd .. && docker compose up -d postgres
   ```

2. Install dependencies:

   ```bash
   uv sync
   ```

3. Run it:

   ```bash
   uv run uvicorn app:app --reload --app-dir src
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
