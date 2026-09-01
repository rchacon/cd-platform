# CD-Server

FastAPI + GraphQL (Strawberry) backend for `cd-webapp`, the React app that
lets users research representatives, legislation, and voting records.

## What it does

`src/cd/server/app.py` mounts a GraphQL endpoint at `/graphql` (built from
the schema in `src/cd/server/schema.py`) plus two plain REST endpoints:
`GET /health` (used by CI and, eventually, an ECS/ALB target group) and
`GET /version`.

`app.py` also registers `CORSMiddleware`, restricted to `POST` (all
GraphQL queries/mutations go over POST) and to the `Authorization`/
`Content-Type` request headers, with `allow_credentials=False` (no
cookie/session auth exists yet -- a future API-key scheme would go over
the already-allowed `Authorization` header instead). Allowed origins
(`settings.CORS_ALLOWED_ORIGINS`) are `cd-webapp`'s deployed production
domain and its local Vite dev server -- GraphiQL's own in-browser
requests are same-origin and unaffected by this either way.

The schema (`src/cd/server/schema.py`) currently exposes:

```graphql
{
  version
  getStates { abbr name seats votingSeats }
  getDistrict(address: "1600 Pennsylvania Ave NW, Washington, DC") { state district }
  getSenators(state: "CA") { bioguideId firstName lastName party }
  getRepresentatives(state: "CA", district: 12) { bioguideId firstName lastName role district }
}
```

`GET /version` returns the same value as a plain REST call --
`{"version": "..."}`, same shape as `cd-api`'s own `GET /version` --
for a quick `curl` check without a GraphQL client. Both read from the
same `VERSION`-file-driven source of truth (`"dev"` when no `VERSION`
file is present, true for every local/test run) via `../cd-lib`'s
shared `read_version()` -- see `cd-lib/README.md` for why that's the
first piece of code shared across `cd-platform`'s Python services, and
the `cd`-namespace-package detail that makes it work. The file itself is
written into `cd/server/VERSION` only in the Dockerfile's `production`
target, from the `CD_SERVER_VERSION` build-arg `cd-server-deploy.yml`
passes as the git-tag version (the `development` target deliberately has
none).

`schema.py`'s resolvers are thin -- each one delegates to a service in
`src/cd/server/services/`, a small layer between the GraphQL resolvers
and whatever they actually depend on (an external HTTP call, a static
table). A **client** in this codebase wraps a specific external
system/protocol and is named after what it's a client *of* -- thin,
handles connection management/serialization/retries, no business logic.
A **service** is what a resolver actually depends on: it may use one or
more clients internally, but also owns real logic (validation,
orchestration, converting an external response shape into a domain
shape) and is named after the capability it provides, not the transport
underneath it. `schema.py` only ever imports from `services/` -- it has
no direct knowledge of `httpx`/`boto3`/the Census geocoder's response
shape.

`getSenators`/`getRepresentatives` are backed by
`services/cd_api_service.py`'s `CdApiService`, cd-server's first real
server-to-server integration with `cd-api`. Internally it holds an
`ApiClient` (a shared ABC, so two transport implementations can't
silently drift apart), picked by `settings.ENVIRONMENT`: `HttpApiClient`
(plain HTTP, for local dev) and `LambdaApiClient` (direct `boto3` invoke
of the real deployed function, bypassing API Gateway entirely -- no
network hop, no `X-Api-Key` needed, since cd-api's own code never checks
that header). `LambdaApiClient` builds a synthetic API-Gateway-shaped
event and calls cd-api's actual Mangum handler with it, so
routing/validation/error-formatting all get exercised exactly as they
would over real HTTP rather than reaching around cd-api's HTTP layer to
call its internal functions directly. `CdApiService` itself is where the
response-shape trust boundary lives: it validates cd-api's raw JSON
against `cd-lib`'s shared `Member`/`MembersResponse` models and hands
`schema.py`'s resolvers real `Member` objects, not a dict the resolver
would otherwise have to parse itself.

`CdApiService`'s `/members` handling is currently a **forward-compat
shim** (cd-platform#104): it sends both the legacy `state`/`district`
query params and the new `filter[state]`/`filter[district]`, and its
`_members()` helper accepts either the old bespoke
`{senators, representatives}` body or a new JSON:API
`{"data": [<member resource>]}` collection. The JSON:API branch is
validated through `cd-lib`'s `CollectionDocument[MemberDetail]` (a
malformed envelope is a `ValidationError`, not a `KeyError` -- the trust
boundary holds for the new shape too), then each resource's `id` becomes
`bioguide_id` and its `state`/`in_office` attributes are dropped.
`_members()` re-applies the chamber/district split client-side either way
-- a backstop so a broken server-side `filter[*]` can't silently return
the whole state's delegation. This ships *before* cd-api's `/members`
flips so cd-server stays up across the switch (which **requires** cd-api's
flip PR to keep `state`/`district` as deprecated accepted params, or the
dual-send 400s on `JsonApiRoute`); the legacy branch, the dual-send, and
`MembersResponse` are removed in a follow-up once cd-api has shipped.

Both the transport `get()`s and the two GraphQL resolvers above are
`async` -- `HttpApiClient` holds a single `httpx.AsyncClient` connection
pool (closed via `CdApiService.aclose()`, called from `app.py`'s FastAPI
`lifespan` on shutdown), and `LambdaApiClient` wraps `boto3`'s own invoke
call (boto3 has no async API at all) in `asyncio.to_thread()` rather than
pulling in a third-party async-boto3 wrapper for what's currently a
single call. This matters concretely for a query requesting both fields
at once -- strawberry runs independent async resolvers concurrently, so
`{ getSenators(...) getRepresentatives(...) }` in one request makes both
cd-api calls in parallel rather than one after the other. Verified
directly: with an injected 0.5s delay per call, the combined query
completed in ~0.5s total, not ~1.0s.

`getStates` (`services/states_service.py`'s `StatesService`) needs no
input -- a static table of USPS state/territory abbreviation -> full
display name, ported from `cd-lookup`'s `StateNames.php` (same 56
entries: 50 states, DC, and 5 territories; `cd-lookup#15`'s original
reasoning still applies here -- the Census geocoder below never spells a
state's name out, even when the input address did). `StatesService` has
no I/O, unlike the other two services -- it's kept as a service anyway
for consistency (`schema.py` depends on a uniform services layer
regardless of whether an implementation happens to be static today; if
`getStates` ever needs to become dynamic, this is the one place that'd
change).

Each `State` also carries `seats` (that state/territory's total House
seats) and `votingSeats` (whether those seats are full voting
Representatives rather than a non-voting Delegate/Resident
Commissioner) -- sourced from `../cd-lib`'s
`SEATS_PER_STATE`/`NON_VOTING_TERRITORIES` (`cd-lib/src/cd/lib/apportionment.py`),
the same 2020-census apportionment table `cd-api` already uses to
validate a `district` query param, shared rather than a second
hand-transcribed copy. `StatesService.get_states()` returns a `dict[str,
StateInfo]` (`StateInfo` a plain `NamedTuple`, not a Pydantic model --
unlike `CdApiService`'s `Member`, there's no external response shape
here to validate against, just cd-server's own static data joined
together, so a Pydantic model would be ceremony without a validation
purpose).

`getDistrict` (`services/geocoder_service.py`'s `GeocoderService`)
resolves a free-text address to a state/district via the Census Bureau's
geocoding API -- a separate integration from `cd-api` entirely (its own
`httpx.AsyncClient` connection pool, also closed via `app.py`'s
lifespan), also ported from `cd-lookup`
(`LookupDistrict.php`'s `get_district()`/`extract_congressional_district()`,
same algorithm: match a `geographies` layer by a `"...Congressional
Districts"` name pattern, extract the embedded Congress number, and
require the same-numbered `CD<n>` field on that same layer rather than
trusting any `CD*` field found -- so a stray/legacy layer can't silently
supply the wrong district, and disagreement between qualifying layers is
treated as unresolvable rather than guessed at). Raises
`NoAddressMatchError`/`AmbiguousAddressError` for a problem with the
address itself, `GeocoderError` for anything else (network failure,
unexpected response shape) -- both surface as normal GraphQL field
errors with a clear message, same "let the raised exception's message
speak for itself" approach `ApiClientError` already uses above, not a
structured/typed error result.

`cd-server` now has its own Postgres database, `cd_customers`, that no
other component touches -- schema managed by Alembic migrations under
`migrations/` (same raw-SQL `op.execute()` idiom as `cd-etl`'s own).
`entrypoint.sh` runs `alembic upgrade head` on container start for local
dev / CI (`docker compose up cd-server`, `make test-server`) and, today,
for the production service task too -- there's no separate step and no
"forgot to migrate" failure mode. That on-boot run is only safe in
production because the ECS service runs **one task at a time**
(`deployment_minimum_healthy_percent: 0`).

The migrate-split (dormant in this repo until cd-infra#67 provisions its
half) moves production to a dedicated one-shot `entrypoint.sh migrate`
ECS task -- `cd-server-deploy.yml` will run it and wait for exit 0
before redeploying the service -- so the service can then move to a
rolling/surge deployment without two briefly-overlapping app tasks
racing `alembic upgrade` against `cd_customers`. Once that lands, three
independent guards keep the long-running service task out of the
migration path: (1) its ECS task definition overrides `entryPoint` to
skip `entrypoint.sh` entirely (mirrors `cd-etl`); (2) cd-infra also
sets `CD_SERVER_MIGRATE_TASK=1` on that task, so the no-arg path skips
the on-boot migrate even if the `entryPoint` override is ever dropped
-- it defaults to running migrations, which is what keeps the split
safe to ship before the one-shot task exists; (3) `migrations/env.py`
takes a **session-scoped** `pg_advisory_lock` (not
`pg_advisory_xact_lock`, which an `autocommit_block()` migration would
drop mid-run), serializing any two runners that still overlap. Because a
rolling deploy means old code briefly runs against the new schema,
migrations here must be **backward-compatible within one deploy**
(expand/contract -- add before you require, stop reading before you
drop). Its only
table today, `users` (`id`, `email`, `created_at`, `last_seen`), is
upserted by `services/users_service.py`'s `UsersService` -- following the
same client/service split as `CdApiService` above: `UsersClient` is the
thin client (owns the `asyncpg` connection pool and the raw upsert SQL,
no JWT knowledge), `UsersService` is the actual service (owns JWT
verification/claim extraction, calls the client). It's wired in via
`app.py`'s `GraphQLRouter(..., context_getter=...)`, run on every GraphQL
request rather than from a resolver: an `Authorization: Bearer <token>`
header, if present, is verified against Cognito's real JWKS (`PyJWKClient`
against `https://cognito-idp.<region>.amazonaws.com/<user_pool_id>/.well-known/jwks.json`,
checking `token_use == "id"` and `aud` against `COGNITO_CLIENT_IDS` --
covering both `cd-webapp`'s prod and local-dev App Clients, which share
one User Pool) and, if valid, the resulting `sub`/`email` are upserted
unconditionally, not throttled to only new users -- a deliberately simple
first pass. A missing or invalid token never blocks the request; no
resolver requires auth yet.
`COGNITO_USER_POOL_ID`/`COGNITO_REGION` unset disables verification
entirely rather than failing startup when `CD_SERVER_ENVIRONMENT` is
`"local"` (the default) -- `make start-server` needs zero AWS setup for
representative-lookup-only local dev; any other environment fails fast at
import instead, same precedent as `get_cd_api_service()`. Note this is
necessary but not sufficient on its own: `cd-webapp` doesn't yet attach
an `Authorization` header to any of its GraphQL calls, so nothing upserts
in practice until that's wired up there, separately. `cd-server` will
still get its own API-key/billing management down the line -- not built
yet.

### Calling cd-api locally

`HttpApiClient`'s default target is `http://host.docker.internal:8001`
(overridable via `CD_API_BASE_URL`) -- reachable from cd-server's own
container via the `extra_hosts` entry in `../docker-compose.yml` (Linux
doesn't resolve `host.docker.internal` by default the way Docker
Desktop does). Port 8001, not cd-api's own README default of 8000 --
that's cd-server's own published port, so running cd-api on 8000 too
would collide with it. Start `cd-api` yourself first, bound to all
interfaces (uvicorn's own default, `127.0.0.1`, isn't reachable from
inside a container):

```bash
cd ../cd-api && uv run uvicorn cd.api.app:app --app-dir src --host 0.0.0.0 --port 8001
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
