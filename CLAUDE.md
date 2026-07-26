# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│     Congress API     │ ──▶ │        cd-etl        │ ──▶ │      PostgreSQL      │
│  (api.congress.gov)  │     │   (Apache Airflow)   │     │                      │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

This is the backend for the `cd-lookup` WordPress plugin. `cd-etl` is an Airflow
DAG (`cd-etl/src/members_etl.py`) that syncs House and Senate members of the
current Congress from api.congress.gov into a Postgres schema (`init.sql`).
`docker-compose.yml` at the repo root runs Postgres locally, applying
`init.sql` via the Postgres image's `docker-entrypoint-initdb.d` mechanism
(only on first container creation with an empty volume — schema changes
require recreating the volume, not just restarting the container).

### DAG pipeline (`congress_members_etl`)

Tasks run in this order, each an Airflow TaskFlow `@task`:

1. `sync_current_congress` — fetches `/congress/current` from the API and
   upserts the `congresses` table. `end_date` is derived as the earliest
   session's `start_date` plus two years, **not** from the API's own
   `endYear` field (that field is a "generalized" label, off by one from the
   actual term-end date).
2. `get_current_congress` — reads "current" back out of the `congresses`
   table (the row whose date range contains today) rather than trusting the
   API's notion of current, so this ETL and the `current_members` view
   share one definition of *which Congress* is current. `current_members`
   additionally filters on `member_terms.end_year`, a second, ETL-independent
   currency check the ETL side has no counterpart for -- see the view's own
   comment in `init.sql` for why (issue #14: year-only precision can't
   resolve same-year departures).
3. `extract_member_summaries` — pages through the **full roster** of the
   Congress using `currentMember=false`, not `currentMember=true`. Using
   `true` would silently drop members who resigned, died, or were expelled
   mid-Congress, and their `end_year` would never get recorded.
4. `filter_members_needing_sync` — compares each member's list-level
   `updateDate` against the stored `members.source_updated_at` and skips the
   per-member detail endpoint for anyone unchanged. This is the main
   API-call-reduction mechanism; the detail endpoint is one HTTP call per
   member.
5. `fetch_member_details` — fetches full profiles for whatever's left, via a
   `ThreadPoolExecutor`.
6. `transform` — builds the `members`/`member_terms` row tuples.
7. `load` — upserts both tables. `ON CONFLICT DO UPDATE` is guarded by
   `WHERE source_hash IS DISTINCT FROM EXCLUDED.source_hash`, so `updated_at`
   only changes on rows that actually changed.

### Data model notes (`init.sql`)

- `member_terms` currently only stores rows for the **current** Congress —
  historical terms aren't retained. `district`: `NULL` = Senator, `0` =
  at-large House seat, `1+` = numbered House district. The item-level
  Congress.gov API omits the `district` field entirely for at-large seats
  (unlike the list endpoint, which returns `0`) — `_term_rows` treats a
  missing value as `0` only for House seats.
- `members.party_history` is a JSONB array mirroring the API's `partyHistory`
  (`[{"party", "source_party_name", "start_year", "end_year"}, ...]`),
  independent of Congress/term boundaries. Party is deliberately **not**
  stored per-term; `current_members` derives each member's current party
  via a `LEFT JOIN LATERAL` picking the entry with the greatest `start_year`.
  This is what lets a mid-Congress party switch show up immediately without
  touching `member_terms`.
- `party_history` entries are sorted by `start_year` before hashing/storing
  (see `_party_history`) rather than trusting the API's array order, so
  `source_hash` doesn't change spuriously if the API ever reorders that
  array.
- There is no `party_type` enum — party values are normalized by the ETL
  (`PARTY_MAP`) to a small canonical set but stored as plain text, since
  Postgres can't validate values inside JSONB anyway.
- Known gap, tracked in issue #3: `current_members` has no Senior/Junior
  Senator distinction, since that's based on continuous years of Senate
  service and isn't derivable from current-Congress-only term data.

### XCom gotcha

`transform`'s return value crosses an Airflow XCom boundary (serialized to
JSON and stored in the metadata DB before `load` runs). `psycopg2.extras.Json`
wrappers cannot survive that — `_wrap_party_history_for_insert` applies the
`Json(...)` wrapper to `party_history` only inside `load`, right before the
actual `execute_values` call, never earlier.

## Git conventions

PRs are merged with a merge commit (`gh pr merge --merge`), not squash or
rebase — preserves the individual commit history from the PR branch.

## Commands

All commands run from `cd-etl/` unless noted otherwise.

```bash
# One-time setup
cp .env.sample .env              # fill in CONGRESS_API_KEY and AIRFLOW_HOME
cd .. && docker compose up -d postgres && cd cd-etl
uv sync
set -a && source ../.env && set +a
uv run airflow db migrate
uv run airflow connections add congressional_postgres \
  --conn-type postgres --conn-host localhost --conn-port 5432 \
  --conn-schema congressional_app --conn-login postgres --conn-password postgres

# Run the DAG
uv run airflow standalone         # UI at http://localhost:8080

# Tests
uv run pytest tests/              # full suite
uv run pytest tests/test_members_etl.py::test_name   # single test
```

`tests/test_upsert_sql.py` needs a live Postgres (`docker compose up -d
postgres`) and skips itself if one isn't reachable at `localhost:5432`; every
other test is a pure unit test with no external dependencies.
`tests/conftest.py` sets a placeholder `CONGRESS_API_KEY` so the module (which
reads it at import time) can be imported without a real key.

CI (`.github/workflows/cd-etl-tests.yml`) runs on every PR: it brings up the
same `docker-compose.yml` Postgres service (reusing `init.sql` as the single
source of truth for schema, rather than a separate CI-only schema
definition), then runs the full test suite via `uv sync --locked`.
