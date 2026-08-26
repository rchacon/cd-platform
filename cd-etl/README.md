# CD-ETL

Airflow ETL that syncs House and Senate members, and House roll call votes,
of the current Congress from [api.congress.gov](https://api.congress.gov)
into the schema defined in `migrations/versions/`.

## What it does

`src/congress_api.py` (session/pagination/concurrent-fetch HTTP helpers --
strictly interfacing with api.congress.gov itself, no DB code),
`src/db.py` (shared Postgres helpers -- `IsolatedTransaction`,
`get_current_congress`, `source_hash`), and `src/congress_models.py`
(Pydantic models for the API's response shapes) are shared, DAG-agnostic
modules every DAG below builds on.

### `congress_members_etl` (`src/members_etl.py`)

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
6. `transform` — validates each raw response into a Pydantic model and
   builds `members`/`member_terms` rows.
7. `load` — upserts both tables; updates are guarded by `source_hash` so
   `updated_at` only bumps when something actually changed.

### `house_votes_etl` (`src/house_votes_etl.py`)

Syncs House roll call votes into `roll_calls`/`roll_call_member_votes`,
populating bills on demand rather than proactively -- see the module's
own docstring for why.

1. `get_current_congress` — same query `congress_members_etl` uses.
2. `extract_house_vote_summaries` — pages through both sessions' vote lists.
3. `filter_votes_needing_sync` — skips already-synced votes and drops
   purely procedural ones.
4. `resolve_bills` — resolves each vote's `bill_id` (syncing the bill on
   demand if it's new), sequentially rather than concurrently so two
   votes referencing the same new bill don't race an insert.
5. `fetch_vote_details` — concurrently fetches each vote's question detail.
6. `sync_member_votes` — processes votes in small batches (bounds peak
   memory to one batch's worth of member-vote casts rather than the whole
   run's, see rchacon/cd-platform#59): concurrently fetches each batch's
   individual member casts, validates and normalizes them (dropping any
   vote whose member-vote fetch failed rather than storing it
   incomplete), and upserts `roll_calls`/`roll_call_member_votes` for
   that batch together in one transaction before moving to the next.

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
   `http://localhost:8080` and log in as `admin` (password from
   `AIRFLOW_ADMIN_PASSWORD` in your `.env`, or `admin` if unset), unpause
   `congress_members_etl`, and trigger it.
   Once it's synced members, unpause and trigger `house_votes_etl` too --
   it looks up `members.bioguide_id` for each vote cast, so members needs
   to run first. `cd-etl/src` is bind-mounted, so DAG edits show up
   without rebuilding.

3. Optionally, seed real data instead of running the DAG. `local_seed.sql`
   is gitignored (not tracked in git -- from a previous run of your own, or
   one a teammate shared with you directly); if you have one, load it:

   ```bash
   docker compose exec -T postgres psql -U postgres -d congressional_app \
     -f - < local_seed.sql
   ```

   Otherwise there's nothing to load yet -- run the DAGs once (step 2
   above; `house_votes_etl` too, if you want its tables in the seed) to
   populate real data first. Once they have, generate `local_seed.sql`
   for next time (only needed when a schema change alters one of these
   tables' own columns, or you want a fresher real-data snapshot).
   `congresses` is deliberately excluded -- migration 0001 already seeds
   it, so a fresh schema always has it regardless of this file:

   ```bash
   docker compose exec -T postgres pg_dump -U postgres -d congressional_app \
     --data-only -t members -t member_terms -t bills -t bill_subjects \
     -t roll_calls -t roll_call_member_votes > local_seed.sql
   ```

## Releasing

Pushing a tag matching `cd-etl-v*` (e.g. `cd-etl-v1.0.0`) triggers
`.github/workflows/cd-etl-deploy.yml`, which builds this same `docker/Dockerfile`'s
`production` target and pushes it to GHCR as `ghcr.io/<owner>/cd-etl`. The
`cd-etl-v` prefix is dropped from the pushed version tag -- tag
`cd-etl-v1.0.0` produces image tags `1.0.0` and `latest`, not
`cd-etl-v1.0.0`. The workflow's first step (`../scripts/check-tag-version.sh`)
hard-fails the deploy if the tag's version doesn't match `pyproject.toml`'s
own `version`; an optional local `pre-push` git hook runs the same check
before the tag is even pushed (`git config core.hooksPath .githooks`, see
the root `CLAUDE.md`).

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
