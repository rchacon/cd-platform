# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, etc.) when working with code in this repository.

## Architecture

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│     Congress API     │ ──▶ │        cd-etl        │ ──▶ │      PostgreSQL      │
│  (api.congress.gov)  │     │   (Apache Airflow)   │     │                      │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

This is the backend for the `cd-lookup` WordPress plugin. `cd-etl` is a set of
Airflow DAGs -- `cd-etl/src/cd/etl/dags/members_etl.py` syncs House and Senate
members of the current Congress from api.congress.gov,
`cd-etl/src/cd/etl/dags/house_votes_etl.py` syncs House roll call votes
(syncing whatever bill each vote references on demand, via
`bills_common.py`), and `cd-etl/src/cd/etl/dags/bills_etl.py` refreshes
already-synced bills' `policy_area`/subjects/title/CRS summary on its own
schedule (see that file's own DAG pipeline section below) -- all into a
Postgres schema managed by Alembic migrations (`cd-etl/migrations/`). `cd-api`
is a FastAPI app (`cd-api/src/cd/api/app.py`) that exposes the
`current_members` view over HTTP for `cd-lookup` to consume, replacing its
current GovTrack HTML scrape -- see `cd-api/README.md`. `cd-server` is a
FastAPI + GraphQL (Strawberry) app (`cd-server/src/cd/server/app.py`) that
will back `cd-webapp`, a separate React repo -- besides a `version` query
and plain REST `/health`/`/version` endpoints (the latter mirroring
`cd-api`'s own `GET /version` shape), it now makes real server-to-server
calls to `cd-api` (`getSenators`/`getRepresentatives`, mirroring
`cd-lookup`'s functionality) via `cd-server/src/cd/server/clients.py` --
two interchangeable async client implementations sharing an `ApiClient`
ABC (`HttpApiClient` for local dev, `LambdaApiClient` bypassing API
Gateway via a direct `boto3` invoke -- wrapped in `asyncio.to_thread()`
since `boto3` itself has no async API -- of the real function in
production) picked by `settings.ENVIRONMENT`; both GraphQL resolvers are
`async` too, so a query requesting multiple fields makes their cd-api
calls concurrently rather than sequentially. See `cd-server/README.md`.
Down the line it will also get its own Postgres
database and issue/manage API keys and billing for authenticated users --
not built yet. Unlike `cd-api`'s Lambda-zip deploy, `cd-server` is
containerized from day one (see below), since it's expected to hold
long-lived state/connections once its own database lands; the intended
production target is an ECS service backed by EC2, provisioned in
`cd-infra`.
`cd-lib` (`cd-lib/src/cd/lib/`) is a shared library the Python services
depend on as a local path dependency (`[tool.uv.sources]`, not a
published package, not a `uv` workspace -- each component keeps its own
independent `pyproject.toml`/`uv.lock`): `version.py`'s `read_version()`
(consumed by `cd-server`), and `models.py`'s `Member`/`MembersResponse`
Pydantic models -- only those two, moved out of `cd-api` so `cd-server`
can validate cd-api's actual responses against the same model cd-api
itself built them from (`cd-api`'s own `VersionResponse`/`ProblemDetail`/
`ValidationProblemDetail` deliberately stayed in `cd-api/src/cd/api/models.py`,
since `cd-server` never touches them -- `cd-lib` is for code that's
actually shared, not a dumping ground for every model cd-api happens to
have; see `cd-lib/README.md`). `cd-etl` doesn't depend on `cd-lib` yet.
Whether to use `editable = true` on the
`[tool.uv.sources]` entry is a real, load-bearing choice, not a style
preference: `cd-server` uses it (fine -- its whole life happens inside a
container whose filesystem is stable between build and run), `cd-api`
deliberately does not (its Lambda zip build has no persistent source
tree at runtime -- `editable = true` was confirmed empirically to
produce only a dangling `.pth` file pointing at the build machine's own
absolute path, not real copied files, silently breaking the deployed
zip). See `cd-lib/README.md`'s "Consuming it" section for the full
explanation. Any component that depends on `cd-lib` must also have no
`cd/__init__.py` of its own (an implicit PEP 420 namespace package, not
a regular one) so its own `cd.<component>` and `cd-lib`'s `cd.lib` --
installed from two physically separate locations -- merge into one
importable `cd` namespace instead of only one of them being visible;
`cd-server` and `cd-api` both already have this (their own
`src/cd/__init__.py` was removed when each adopted `cd-lib`), `cd-etl`
still has its and would need the same removal if/when it adopts
`cd-lib` too. A component built in Docker needs its build context to be
the repo root, not its own directory, so `cd-lib` is reachable at all
(`cd-server/docker/Dockerfile` is the first example of this); a
Lambda-zip-deployed component instead just needs the whole repo checked
out in CI, no Dockerfile/COPY step involved.
`docker-compose.yml` at the repo root runs Postgres, plus a `cd-etl` service
built from `cd-etl/docker/Dockerfile` -- the same image also pushed to GHCR (see
`cd-etl/README.md`'s Releasing section) on a `cd-etl-v*` tag, so local dev
and deployment run identically rather than two commands that could drift.
Docker is the only local dependency for `cd-etl` -- no `uv`/Python needed on
the host (see the root `Makefile`'s `start-etl`/`test-etl` targets). A
`cd-server` service follows the identical pattern (`cd-server/docker/Dockerfile`,
pushed to GHCR on a `cd-server-v*` tag, `make start-server`/`test-server`) --
see `cd-server/README.md`. The
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
8. `load` — upserts `members`/`member_terms`. `ON CONFLICT DO UPDATE` is
   guarded by `WHERE source_hash IS DISTINCT FROM EXCLUDED.source_hash`, so
   `updated_at` only changes on rows that actually changed.
9. `load_crosswalk` — a plain guarded `UPDATE` for the crosswalk rows, never
   an upsert (this task never creates a `members` row itself -- rows for a
   `bioguide_id` not yet synced by `load` simply match nothing). A separate
   `@task` from `load`, not a second commit folded into it, specifically so
   a crosswalk-specific failure gets Airflow's own task-level retries
   (`default_args={"retries": 2}`) and shows up as a failed task run,
   rather than being caught and swallowed into one log line. Still can't
   block or roll back the member/term sync: `load(...) >> load_crosswalk(...)`
   makes it strictly downstream of `load` already having committed, not
   just downstream of `transform`'s output.

### DAG pipeline (`bills_etl`)

Resolves issue #52: `house_votes_etl`'s `get_or_sync_bill()` is sync-once --
on a cache hit it returns the existing `bill_id` without re-fetching, even
though a bill's `policy_area` can be reassigned and its `legislativeSubjects`
can be added or removed over its lifetime. `bills_etl` is the missing
refresh path, on its own `@daily` schedule:

1. `get_current_congress` — delegates to `congress_api.get_current_congress()`,
   shared with `house_votes_etl`'s identical task (both take no upstream
   argument, unlike `members_etl`'s own copy -- see that module's task of
   the same name).
2. `extract_known_bills` — `SELECT bill_type, bill_number FROM bills WHERE
   congress = %s AND synced_at < NOW() - (%s * INTERVAL '1 day')`, the
   second parameter being `REFRESH_MIN_INTERVAL_DAYS` (7). Deliberately
   refresh-only, not a discovery DAG: it only re-syncs bills already
   present in the `bills` table, the same "only sync bills something
   actually references" precedent `house_votes_etl`'s own module docstring
   already established (of 18,140 bills in the 119th Congress, only a few
   hundred are ever referenced by a vote -- proactively discovering every
   bill in a Congress via a new `/bill/{congress}` list call would
   reintroduce that exact wasted-API-call problem). New-bill discovery
   stays where it already is: `house_votes_etl`'s (and, eventually, a
   future `senate_votes_etl`'s) on-demand `get_or_sync_bill()` path. The
   `synced_at` cutoff is a coarse staleness backoff on top of that: most
   bills settle down once enacted/failed/vetoed and won't meaningfully
   change again, but this schema has no bill-status field to detect that
   directly, so a bill simply isn't re-checked again until at least
   `REFRESH_MIN_INTERVAL_DAYS` have passed since its last successful sync
   -- caps the recurring daily API/DB-write volume at the cost of up to
   that many days' staleness on a genuinely-still-active bill.
3. `refresh_bills` — calls `bills_common.sync_bill()` for each known bill
   via `congress_api.fetch_concurrently` (`REFRESH_BATCH_WORKERS`, 5 at
   once), each worker opening and closing its own connection rather than
   sharing one -- `sync_bill`'s cursor/commit calls aren't safe to run
   concurrently on a single psycopg2 connection, unlike the pure-HTTP
   concurrent fetches this pattern is normally used for elsewhere
   (`fetch_vote_details`, `fetch_member_votes`). Unlike `house_votes_etl`'s
   `resolve_bills` (which stays sequential specifically to avoid two votes
   in the same batch racing to insert the *same* not-yet-existing bill),
   there's no such race here -- every row in `known_bills` already exists.
   One bill's failure is logged and skipped, not fatal to the run.

`bills_etl` is deliberately **not** triggered by or triggering
`house_votes_etl`/a future `senate_votes_etl`, and there's no ordering
guarantee between them (no `TriggerDagRunOperator`/Airflow Dataset -- no
precedent for either in this codebase). Once discovery and refresh are
split this way, a vote-sync DAG no longer depends on `bills_etl` having
just run: it still does its own on-demand fetch for any bill not yet known,
and staleness of an already-known bill's `policy_area`/subjects is a
downstream reader's problem (`cd-api`/`cd-lookup`), not a vote-sync
correctness problem -- the same loosely-coupled precedent
`extract_legislators_crosswalk` above already set for a related-but-
independent sync.

Both `house_votes_etl.get_or_sync_bill()` (on a cache miss) and
`bills_etl.refresh_bills` (unconditionally, for every known bill) call the
same fetch+upsert function, `bills_common.sync_bill()` -- it fetches a
bill's detail, `/subjects`, and `/summaries` sub-resources concurrently,
storing `title`, `policy_area`, the most recent CRS summary (by
`actionDate`, since Congress.gov issues a new one at each legislative
stage, and skipping any entry with null/empty `text` rather than picking
one that has none), and a full replace of `bill_subjects`. `/summaries`'s
own failure degrades to "no CRS summary this sync" rather than failing
the whole call -- only detail/subjects are load-bearing for `bill_id`, a
hard FK target for `roll_calls`. Both the `bills` upsert (via `COALESCE`
against the existing row) and the `bill_subjects` replace (skipped
entirely when the fetched list is empty) are guarded against a
degraded/empty response nulling out or wiping previously-good data --
`sync_bill` now runs repeatedly against the same bill (`bills_etl`'s
daily refresh), not just once, so a single bad response can no longer
overwrite good data until some later refresh happens to restore it.

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
`cd-server/` needs Docker only, same as `cd-etl` -- see `cd-server/README.md`.

```bash
# One-time setup
cp .env.sample .env   # fill in CONGRESS_API_KEY
git config core.hooksPath .githooks  # optional: catches a cd-etl-v*/cd-api-v*/
                                      # cd-server-v* tag/pyproject.toml version
                                      # mismatch before CI does. Repoints ALL
                                      # git hooks to .githooks, so skip this if
                                      # you use another hooks framework
                                      # (husky, lefthook, pre-commit, etc.)
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

make start-server      # docker compose up --build cd-server -- GraphiQL at
                        # http://localhost:8000/graphql, health check at
                        # http://localhost:8000/health
make test-server       # docker compose run --rm cd-server uv run pytest tests/
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
`.github/workflows/cd-server-tests.yml`/`cd-server-deploy.yml` mirror
`cd-etl`'s own two workflows exactly (Docker/GHCR, `cd-server-v*` tags,
`scripts/check-tag-version.sh`), since `cd-server` follows `cd-etl`'s
container-deploy model rather than `cd-api`'s Lambda-zip one -- see the
Architecture section above for why.
