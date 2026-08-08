# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, etc.) when working with code in this repository.

## Architecture

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│     Congress API     │ ──▶ │        cd-etl        │ ──▶ │      PostgreSQL      │
│  (api.congress.gov)  │     │   (Apache Airflow)   │     │                      │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

This is the backend for the `cd-lookup` WordPress plugin. `cd-etl` is an Airflow
DAG (`cd-etl/src/members_etl.py`) that syncs House and Senate members of the
current Congress from api.congress.gov into a Postgres schema managed by
Alembic migrations (`cd-etl/migrations/`). `cd-api` is a FastAPI app
(`cd-api/src/app.py`) that exposes the `current_members` view over HTTP for
`cd-lookup` to consume, replacing its current GovTrack HTML scrape -- see
`cd-api/README.md`.
`docker-compose.yml` at the repo root runs Postgres, plus a `cd-etl` service
built from `cd-etl/docker/Dockerfile` -- the same image also pushed to GHCR (see
`cd-etl/README.md`'s Releasing section) on a `cd-etl-v*` tag, so local dev
and deployment run identically rather than two commands that could drift.
Docker is the only local dependency for `cd-etl` -- no `uv`/Python needed on
the host (see the root `Makefile`'s `start-etl`/`test-etl` targets). The
container's entrypoint applies both Airflow's own metadata migrations and
this project's own schema migrations (`cd-etl/migrations/`) on every start,
so there's no separate manual migration step and no "forgot to migrate"
failure mode. Airflow's own metadata lives in a separate `airflow_metadata`
database on the same Postgres instance (not its SQLite default), matching
how production's RDS instance is designed.
A gitignored `local_seed.sql` (a `pg_dump --data-only` snapshot of
`members`/`member_terms`/`bills`/`bill_subjects`/`roll_calls`/
`roll_call_member_votes`) can be loaded after the schema exists to seed
real data instead of re-running the DAGs. `congresses` is deliberately
excluded -- migration 0001 already seeds it (`INSERT ... ON CONFLICT DO
NOTHING`), so a fresh schema always has it regardless of this file.
`pg_dump` orders a multi-table `--data-only` dump by foreign-key
dependency automatically, so this single command produces a
restore-safe file without needing per-table ordering by hand. Generate
it with (only needed when a schema change alters one of these tables'
own columns, or you want a fresher real-data snapshot):

```bash
docker compose exec -T postgres pg_dump -U postgres -d congressional_app --data-only \
  -t members -t member_terms -t bills -t bill_subjects -t roll_calls -t roll_call_member_votes \
  > local_seed.sql
```

Then load it:

```bash
docker compose exec -T postgres psql -U postgres -d congressional_app -f - < local_seed.sql
```

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
   comment in `cd-etl/migrations/versions/0001_initial_schema.py` for why
   (issue #14: year-only precision can't resolve same-year departures).
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
6. `extract_legislators_crosswalk` — fetches and parses
   `unitedstates/congress-legislators`'s `legislators-current.yaml`, a
   separate public/unauthenticated source (no relation to api.congress.gov)
   used to resolve `members.lis_member_id` (needed to key a future Senate
   votes DAG, which identifies members by LIS id, not `bioguideId`) and
   `members.senate_state_rank` (`SENIOR`/`JUNIOR`, resolving issue #3 --
   sourced from that file's editorially-maintained per-term `state_rank`
   rather than derived from continuous-service history). Has no upstream
   dependency on `current_congress`/member details, so Airflow runs it
   concurrently with the rest of the chain above. Never raises: a
   broken/unreachable source degrades to "no crosswalk update today," not a
   failed/retried run of the member sync `cd-lookup` depends on.
7. `transform` — builds the `members`/`member_terms` row tuples, plus a
   third `crosswalk` row list (`(bioguide_id, lis_member_id,
   senate_state_rank)`) from step 6's raw YAML entries.
8. `load` — upserts `members`/`member_terms` (`ON CONFLICT DO UPDATE` guarded
   by `WHERE source_hash IS DISTINCT FROM EXCLUDED.source_hash`, so
   `updated_at` only changes on rows that actually changed), then commits a
   **separate**, plain guarded `UPDATE` for the crosswalk rows -- never an
   upsert, since this task never creates a `members` row itself. That
   second commit is deliberately isolated from the first: a crosswalk-
   specific failure is caught, logged, and rolled back on its own, without
   touching the member/term data the first commit already landed.

### Data model notes (`cd-etl/migrations/versions/0001_initial_schema.py`)

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
- `members.senate_state_rank` (`SENIOR`/`JUNIOR`, NULL for House members)
  resolves issue #3's Senior/Junior Senator distinction, exposed on
  `current_members` as `state_rank`. Sourced from
  `unitedstates/congress-legislators`'s `legislators-current.yaml` (see the
  `congress_members_etl` pipeline's `extract_legislators_crosswalk` step
  above) rather than derived from continuous-service history -- that
  upstream file already carries an editorially-maintained `state_rank` per
  Senate term, correctly resolving tie-break cases (prior chamber/
  gubernatorial service, alphabetical order) this project would otherwise
  have to approximate.

### XCom gotcha

`transform`'s return value crosses an Airflow XCom boundary (serialized to
JSON and stored in the metadata DB before `load` runs). `psycopg2.extras.Json`
wrappers cannot survive that — `_wrap_party_history_for_insert` applies the
`Json(...)` wrapper to `party_history` only inside `load`, right before the
actual `execute_values` call, never earlier.

## Git conventions

PRs are merged with a merge commit (`gh pr merge --merge`), not squash or
rebase — preserves the individual commit history from the PR branch.
After merging, delete the branch both locally and remotely
(`gh pr merge --merge --delete-branch` does both in one step).

When addressing review comments on an open PR, break the fixes up into
separate commits along logical lines (one commit per distinct issue/fix,
not one commit for everything) rather than a single catch-all commit, and
reply to each review comment on GitHub referencing the specific commit
hash that addressed it, formatted as a hyperlink to the commit rather than
just backticked text (e.g. "Fixed in
[abc1234](https://github.com/<owner>/<repo>/commit/abc1234).") -- keeps
the review thread traceable to the exact change that resolved it, one
click away, rather than a generic "addressed" reply pointing at the whole
PR.

When *submitting* a code review on a PR, post each finding as its own
separate inline review comment (anchored to the specific file/line via
`gh api repos/{owner}/{repo}/pulls/{number}/comments`, not a single bundled
`gh pr comment`) -- a combined comment listing every finding only supports
one flat reply thread, making it impossible to reply to (or resolve)
individual findings separately later.

## Commands

Run from the repo root unless noted otherwise. `cd-api/` has its own
independent, non-Docker command set (`uv sync`, `uv run uvicorn`,
`uv run pytest`) run from `cd-api/` instead -- see `cd-api/README.md`.

```bash
# One-time setup
cp .env.sample .env   # fill in CONGRESS_API_KEY
git config core.hooksPath .githooks  # optional: catches a cd-etl-v*/cd-api-v*
                                      # tag/pyproject.toml version mismatch
                                      # before CI does. Repoints ALL git hooks
                                      # to .githooks, so skip this if you use
                                      # another hooks framework (husky,
                                      # lefthook, pre-commit, etc.)
make start-etl        # docker compose up -d postgres && docker compose up
                       # --build cd-etl -- builds the image, applies both
                       # Airflow's own and this project's migrations, starts
                       # the DAG -- UI at http://localhost:8080

# Optionally seed real data instead of running the DAG
docker compose exec -T postgres psql -U postgres -d congressional_app \
  -f - < local_seed.sql

# Tests (tests/ is bind-mounted, so this doesn't need uv/Python on the host)
make test-etl                                  # docker compose run --rm -e PGDATABASE=congressional_app_test cd-etl uv run pytest tests/
make test-etl TEST=test_members_etl.py::test_name
```

`tests/test_upsert_sql.py` needs a live Postgres and skips itself if one
isn't reachable; every other test is a pure unit test with no external
dependencies. `tests/conftest.py` sets a placeholder `CONGRESS_API_KEY` so
the module (which reads it at import time) can be imported without a real
key.

`make test-etl` targets a dedicated `congressional_app_test` database (a
sibling of `congressional_app` and `airflow_metadata` in the same Postgres
container, created by `cd-etl/docker/init-test-db.sh`) rather than the
real dev database -- isolates tests from real dev-seeded data and from
`make start-etl`'s long-running service, so the two no longer race each
other's migrations (`cd-platform#16`). `cd-api`'s tests share this same
database (see `cd-api/README.md`) -- its schema is only ever applied by
`cd-etl`'s side, so `cd-api`'s tests need `make test-etl` to have run at
least once first.

`cd-etl/docker/Dockerfile` is multi-stage: `production` (what ships to GHCR) has
no test dependencies at all -- its `base` stage's `uv sync --locked
--no-dev` never installs `pytest`, and `UV_NO_SYNC=1` stops `uv run` from
silently re-syncing the full lockfile back in at runtime (which it
otherwise does on every invocation, regardless of what flags the original
`uv sync` used). `development` (what `make start-etl`/`test-etl` and CI
both build) layers a second, full `uv sync --locked` and `tests/` on top.

CI (`.github/workflows/cd-etl-tests.yml`) runs on every PR: the `test` job
runs `make test-etl` -- the exact same command local dev uses, one less
thing that can drift between the two. The `docker-build` job builds the
`production` target specifically and smoke-tests it (health endpoint +
`congress_members_etl` actually discovered), catching a broken production
image before a `cd-etl-v*` release tag ever gets cut -- a plain `docker
build` alone wouldn't catch a container that builds fine but fails to
actually run.
`.github/workflows/cd-etl-deploy.yml` builds (`--target production`) and
pushes `cd-etl/`'s image to GHCR (`ghcr.io/<owner>/cd-etl`, tagged with the
version and `latest`) on a `cd-etl-v*` tag push -- no AWS credentials
involved; the EC2 side polls for new images via Watchtower rather than CI
pushing to it directly.
`.github/workflows/cd-api-tests.yml` runs an analogous (non-Docker) pipeline
for `cd-api/`.
