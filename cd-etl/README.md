# CD-ETL

Airflow ETL that syncs House and Senate members of the current Congress from
[api.congress.gov](https://api.congress.gov) into the `members`/`member_terms`
tables defined in `migrations/versions/0001_initial_schema.py`.

## What it does

`src/members_etl.py` defines a single TaskFlow DAG, `congress_members_etl`:

1. `sync_current_congress` — fetches `/congress/current` and upserts the
   `congresses` table (so the app never relies on a hardcoded Congress number).
2. `get_current_congress` — reads "current" back from `congresses` (date range
   containing today), so this ETL and the `current_members` view share
   one definition of "current."
3. `extract_member_summaries` — pages through the full roster of the current
   Congress (including members who've since resigned, died, or been expelled).
4. `filter_members_needing_sync` — skips the expensive per-member detail call
   for anyone whose `updateDate` hasn't changed since our last sync.
5. `fetch_member_details` — fetches full profile data for whatever's left.
6. `transform` — builds `members`/`member_terms` rows.
7. `load` — upserts both tables; updates are guarded by `source_hash` so
   `updated_at` only bumps when something actually changed.

## Prerequisites

- Docker -- the only local dependency. No `uv`/Python install needed;
  everything (dependencies, Airflow's own metadata migrations, this
  project's own schema migrations) runs inside the `cd-etl` container
  defined in `../docker-compose.yml`, built from `docker/Dockerfile` -- the
  same image (also pushed to GHCR on a `cd-etl-v*` tag, see below) local
  dev and deployment both run.
- A free API key from [api.congress.gov](https://api.congress.gov)

## Setup

1. Copy the env template and fill in your API key:

   ```bash
   cp ../.env.sample ../.env
   ```

2. Start everything (from the repo root):

   ```bash
   make start-etl
   ```

   This builds the image, applies both Airflow's own metadata migrations
   and this project's schema migrations, then starts the API server,
   scheduler, and dag-processor together. Open the UI at
   `http://localhost:8080`, unpause `congress_members_etl`, and trigger it.
   `cd-etl/src` is bind-mounted, so DAG edits show up without rebuilding.

3. Optionally, seed real data instead of running the DAG. `local_seed.sql`
   is gitignored (not tracked in git -- from a previous run of your own, or
   one a teammate shared with you directly); if you have one, load it:

   ```bash
   docker compose exec -T postgres psql -U postgres -d congressional_app \
     -f - < local_seed.sql
   ```

   Otherwise there's nothing to load yet -- run the DAG once (step 2 above)
   to populate real data first. Once it has, generate `local_seed.sql` for
   next time (only needed when a schema change alters `members`/
   `member_terms`'s own columns, not for unrelated schema changes):

   ```bash
   docker compose exec -T postgres pg_dump -U postgres -d congressional_app \
     --data-only -t members -t member_terms > local_seed.sql
   ```

## Releasing

Pushing a tag matching `cd-etl-v*` (e.g. `cd-etl-v1.0.0`) triggers
`.github/workflows/cd-etl-deploy.yml`, which builds this same `docker/Dockerfile`'s
`production` target and pushes it to GHCR as `ghcr.io/<owner>/cd-etl`. The
`cd-etl-v` prefix is dropped from the pushed version tag -- tag
`cd-etl-v1.0.0` produces image tags `1.0.0` and `latest`, not
`cd-etl-v1.0.0`.

## Testing

```bash
make test-etl
make test-etl TEST=test_members_etl.py::test_name
```

`docker/Dockerfile` is multi-stage: `production` (what actually ships, built above)
has no test dependencies at all, while `development` (what `make start-etl`/
`test-etl` build) additionally installs `pytest` and copies `tests/` in --
so this also doesn't need `uv`/Python on the host. The entrypoint applies
both Airflow's own and this project's own migrations before every run, so
the schema is always current -- there's no "forgot to migrate" failure mode
to worry about here.

`make test-etl` runs against a dedicated `congressional_app_test` database
(a sibling of `congressional_app` in the same Postgres container, created
by `docker/init-test-db.sh`) -- isolated from both the real dev-seeded data
and from `make start-etl`'s long-running service, so the two can run
concurrently without racing each other's migrations (cd-platform#16).
