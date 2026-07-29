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

- [uv](https://docs.astral.sh/uv/)
- Docker (for the Postgres container defined in `../docker-compose.yml`)
- A free API key from [api.congress.gov](https://api.congress.gov)

## Setup

1. Copy the env template and fill in your API key:

   ```bash
   cp ../.env.sample ../.env
   ```

   Set `CONGRESS_API_KEY` to your key, and `AIRFLOW_HOME` to an absolute path
   for `cd-etl/.airflow` (this scopes Airflow's metadata DB/logs/connections
   to this repo instead of the global `~/airflow`).

2. Start Postgres:

   ```bash
   cd .. && docker compose up -d postgres
   ```

3. Install dependencies:

   ```bash
   uv sync
   ```

4. Apply the schema:

   ```bash
   uv run alembic upgrade head
   ```

   Optionally, seed real data instead of running the DAG, from a gitignored
   `local_seed.sql` (a `pg_dump --data-only` snapshot of `members`/
   `member_terms`):

   ```bash
   docker compose exec -T postgres psql -U postgres -d congressional_app \
     -f - < ../local_seed.sql
   ```

   Regenerate `local_seed.sql` with:

   ```bash
   docker compose exec -T postgres pg_dump -U postgres -d congressional_app \
     --data-only -t members -t member_terms > ../local_seed.sql
   ```

   Only needed when a schema change alters those two tables' own columns,
   not for unrelated schema changes.

5. Initialize Airflow (one-time) and add the Postgres connection:

   ```bash
   set -a && source ../.env && set +a
   uv run airflow db migrate
   uv run airflow connections add congressional_postgres \
     --conn-type postgres \
     --conn-host localhost \
     --conn-port 5432 \
     --conn-schema congressional_app \
     --conn-login postgres \
     --conn-password postgres
   ```

6. Run it:

   ```bash
   uv run airflow standalone
   ```

   This starts the API server, scheduler, and dag-processor together and
   prints an auto-generated admin login. Open the UI (default
   `http://localhost:8080`), unpause `congress_members_etl`, and trigger it.

## Testing

```bash
uv run pytest tests/
```

Most tests are pure unit tests with no dependencies. `tests/test_upsert_sql.py`
exercises the real `source_hash` upsert guard against Postgres and skips
itself if `docker compose up -d postgres` hasn't been run.
