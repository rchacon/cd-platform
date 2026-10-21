# cd-api (REST API, `/v1`)

REST API that `cd-lookup` (the WordPress plugin) consumes for representative
lookups, replacing its current GovTrack HTML scrape. Exposes the
`current_members` view (defined in
`../cd-etl/migrations/versions/0001_initial_schema.py`) over HTTP as JSON.

This used to be its own `cd-api/` component deployed as a Lambda behind API
Gateway. It now lives in cd-server as the `cd.api` package
(`src/cd/api/`, a sibling of `src/cd/server/`), and cd-server mounts its
FastAPI app at `/v1` as a sub-application (`src/cd/server/app.py`). It keeps
its own exception handlers and its own OpenAPI document
(`/v1/openapi.json`), and its imports are still `from cd.api...`. API
Gateway still fronts it at `https://api.civicdog.com/v1`, enforcing the
`X-Api-Key` and usage plan before forwarding to cd-server.
`src/cd/server/origin_guard.py` rejects any `/v1` request that didn't come
through API Gateway, identified by a shared `X-Origin-Verify` secret
(`CD_API_ORIGIN_SECRET`). Without it, cd-server's own hostname would serve
`/v1` with no key at all. Paths below are relative to `/v1`.

## What it does

`src/cd/api/app.py` defines a FastAPI app. Every member endpoint --
the list and the two by-id resources -- now speaks
[JSON:API](https://jsonapi.org/); only `GET /version` keeps a bespoke
shape.

```
GET /members?filter[state]=GA
-> { "data": [ { "type": "member", "id": "<bioguide_id>",
                 "attributes": { ...see below... } }, ... ] }

GET /members?filter[state]=GA&filter[chamber]=house
GET /members?filter[state]=GA&filter[district]=5
```

An **honest collection**: `filter[state]` is required (it's the only
required filter), and `filter[chamber]` (`senate`/`house`, case-insensitive
-- `house` folds in non-voting Delegates and the Resident Commissioner)
and `filter[district]` (`0` at-large, `1+` numbered) each narrow the set
further, composably. `filter[district]=5` returns *only* House members in
district 5 -- Senators have no district and are never bundled in (unlike
the old shape). "Who represents this address" is now two calls (one per
chamber), which `cd-server` already makes concurrently.

For the six non-voting-delegate jurisdictions (DC, PR, GU, VI, AS, MP),
`filter[district]=98` -- the U.S. Census Bureau's FIPS "nonvoting
delegate" code, which its geocoder emits for those addresses -- is
accepted as an alias for the at-large `0`, so a caller that geocodes for
itself (e.g. `cd-lookup`) doesn't have to translate. `98` is still a
`404` for any other state (cd-platform#72).

Each `member` resource's `attributes` is the shared `MemberDetail` shape
-- `first_name`, `middle_name`, `last_name`, `nickname`, `suffix` (the
individual name parts, passed through as-is -- the API does not derive a
combined display name; that's left to the client), `role` (whatever
Congress.gov's `member_type` records for that seat -- `"Senator"`,
`"Representative"`, `"Delegate"` for a non-voting territory seat (DC,
American Samoa, Guam, Northern Mariana Islands, or the US Virgin
Islands), or `"Resident Commissioner"` specifically for Puerto Rico's
non-voting seat), `party`, `phone`, `website`, `photo_url`, `district`
(`member_terms.district`'s own `null`/`0`/`1+` convention -- `null` for a
Senator, `0` for an at-large House seat, `1+` for a numbered one),
`state`, and `in_office` (always `true` for this list -- it's
sitting-only; `GET /members/{bioguide_id}` is the one that also serves
departed members). The bioguide id is the resource `id`, not an
attribute.

`filter[state]` for a state with no seats in the apportionment table
returns `404`. A `filter[district]` that doesn't exist for that state
(validated against real House apportionment by
`max_valid_district`/`is_valid_district` in
`src/cd/api/routes/members.py`, built on `../cd-lib`'s shared
`SEATS_PER_STATE` table -- see `cd-lib/README.md`) also returns `404` --
e.g. `filter[district]=99` for a 14-district state. A `filter[district]`
that *does* exist but is currently vacant returns `200` with `data`
empty, distinct from the `404` above.

Errors from every member endpoint are JSON:API error documents
(`Content-Type: application/vnd.api+json`, body
`{"errors": [{"status", "title", "detail", "source"?}]}`) --
`src/cd/api/jsonapi.py`, via `JsonApiRoute`. `GET /version` alone still
follows [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) ("Problem
Details for HTTP APIs", `application/problem+json`), handled by
`src/cd/api/problem.py`'s `problem_response`.

`src/cd/api/db.py` queries `current_members` and the bill tables through
an asyncpg pool (`CD_API_PG_POOL_SIZE`, default 5), and the routes are
`async def`. The pool is opened at startup by cd-server's lifespan and
closed at shutdown; Starlette never runs a mounted app's own lifespan, so
cd.api's lifespan only runs when the app is served on its own (its tests).
The pool opens no connections up front (`min_size=0`), so an unreachable
`congressional_app` fails `/v1` requests (500) rather than cd-server's
boot; startup probes it once and logs a warning if it can't connect. Queries use `:name` placeholders through `cd-lib`'s
`cd.lib.sql.named()`. `GET /bills`' Titan embed (boto3, no async API)
runs on the threadpool via `run_in_threadpool`. It reads
its own `CD_API_PGHOST`/`CD_API_PGPORT`/`CD_API_PGDATABASE`/`CD_API_PGUSER`/
`CD_API_PGPASSWORD`, not the plain `PG*` names, which cd-server's
`settings.py` already uses for its `cd_customers` database. `src/cd/api/transform.py` holds the pure row -> JSON-shape
functions. `../cd-lib/src/cd/lib/models.py` holds the Pydantic models
documenting those same request/response shapes (`response_model=`/
`responses=` on each route in `src/cd/api/app.py`), so the OpenAPI spec
exported on release (see Releasing below) actually reflects what the API
returns, including error bodies -- shared with `cd-server` via `cd-lib`
(see `../cd-lib/README.md`).

`GET /version` returns `{"version": ...}`, read from cd-server's own
`src/cd/server/VERSION` file: this package ships in cd-server's image, so it
reports cd-server's version. cd-server's Dockerfile writes that file at
build time, so it always reads `"dev"` locally.

### JSON:API endpoints

`GET /members`, `GET /members/{bioguide_id}`,
`GET /members/{bioguide_id}/votes` and `GET /bills` are
[JSON:API](https://jsonapi.org/): `Content-Type: application/vnd.api+json`,
a `data` document holding resource object(s) with `type` + string `id`,
typed `attributes`, and `relationships` carrying resource *linkage*
(`{type, id}` pointers). Errors are JSON:API error documents
(`{"errors": [{"status", "title", "detail", "source"?}]}`, same media
type). Compliant with the spec's MUSTs -- including `400` on an
unsupported query parameter (`include`, `sort`, `fields[...]`, `page[...]`,
anything undeclared) and `415`/`406` on a parametrized JSON:API media
type -- but **not** the optional features: no `included`/`?include=`, no
sparse fieldsets, no relationship `links` or their endpoints (`roll_call`
and `bill` linkage point at resource types with no URL of their own yet),
no pagination/`sort`, no top-level `jsonapi` object. The one real caller
(`cd-server`) runs a fixed two-call merge and needs none of that. Wire
models (`Resource`/`Document`/`CollectionDocument`/`Relationship`/
`ResourceIdentifier`) live in `../cd-lib/src/cd/lib/jsonapi.py`; the HTTP
layer (media type, error documents, `JsonApiRoute` strictness) is
`src/cd/api/jsonapi.py`. `GET /version` is the only endpoint that keeps a
bespoke (non-JSON:API) shape.

```
GET /members/{bioguide_id}
-> { "data": { "type": "member", "id": "K000401",
               "attributes": { ...the same MemberDetail attributes the
                               list returns... "state": "CA",
                               "in_office": true } } }
```

Serves any member of the current Congress, sitting *or* departed mid-term
(`in_office: false`, so a bookmarked page keeps resolving after a
resignation) -- the one difference from the `GET /members` list, which is
sitting-only. `404` only when the id has no current-Congress term at all
(e.g. a member of a past Congress). Same `member` resource shape as the
list; identity is the resource `id`, not an attribute.

```
GET /members/{bioguide_id}/votes?filter[bill]=119-hr-2616,119-s-5
-> { "data": [
     { "type": "roll_call_vote", "id": "119-house-1-327:K000401",
       "attributes": { "vote_cast": "YEA", "vote_question": "On Passage",
                       "result": "Passed", "vote_date": "2026-05-20" },
       "relationships": {
         "member":    { "data": { "type": "member",    "id": "K000401" } },
         "roll_call": { "data": { "type": "roll_call", "id": "119-house-1-327" } },
         "bill":      { "data": { "type": "bill",      "id": "119-hr-2616" } } } } ],
     "meta": { "bills_without_votes": ["119-s-5"] } }
```

This member's roll-call votes across a caller-supplied set of bills -- the
companion to `GET /bills`. Each `roll_call_vote` resource is one
cast position in one roll call; `id` is
`<congress>-<chamber>-<session>-<vote_number>:<bioguide_id>`. Its `bill`
relationship is a **denormalised** edge (a vote reaches a bill *through*
its `roll_call`) carried directly so a caller can group votes by
`relationships.bill.data.id` -- the canonical bill id (`bills.bill_key`,
cd-etl migration 0006) -- to merge with search results, without
fetching the `roll_call`. `vote_question`/`result`/`vote_date` are
likewise denormalised from the roll call: there's no `included`, so what
a client needs to render a vote rides along on the vote.

`filter[bill]` is a required, comma-separated list of 1-50 bill ids -- a
JSON:API relationship filter on the resource's `bill` relationship --
passed back verbatim from a search response. (JSON:API is agnostic about
filter strategy, so `filter[roll_call.bill]`, the traversal path, would
be equally compliant.) Votes are ordered by requested bill, then
oldest-first within a bill. A requested id that names a **synced bill
the member never voted on** is
listed in `meta.bills_without_votes` (not as a resource); a well-formed
id for a bill cd-api **hasn't synced** appears in neither `data` nor
`meta`. A malformed id (not `<congress>-<type>-<number>`) is a `400`; an
unknown member is a `404`, on the same rule as the parent
route.

### `GET /bills` (JSON:API)

```
GET /bills?filter[query]=schools+telling+parents&page[size]=10
-> { "data": [
     { "type": "bill", "id": "119-hr-2616",
       "attributes": { "congress": 119, "bill_type": "HR", "bill_number": 2616,
                       "title": "...", "policy_area": "Education",
                       "crs_summary": "<p>...</p>" },
       "meta": { "matches": [ { "via": "policy_area" } ] } },
     ... ],
     "meta": { "query": "schools telling parents" } }
```

Semantic search over synced bills (cd-platform#9): embeds `filter[query]`
via Bedrock, matches a bill's `policy_area` / legislative subjects first
(tier 1, exact controlled-vocabulary match), then fills remaining slots
with tier-2 cosine similarity against each bill's summary embedding above
a relevance floor -- so it can return fewer than `page[size]` (even
zero). Side-effect-free and cacheable on the query alone.

Each `bill` resource's `id` is the canonical `bills.bill_key`
(cd-etl migration 0006) -- pass it to `GET /members/{bioguide_id}/votes`'s
`filter[bill]`. Each resource's **`meta.matches`** says why that bill
surfaced for *this* search -- a list of `{"via": ...}` where `via` is
`policy_area` / `subject` (exact controlled-vocabulary match) or
`summary` (CRS-summary embedding). It's per-resource `meta`, **not an
attribute** (the bill's place in this search, not the bill -- the `Bill`
attributes model is reused as-is by any future `bill` endpoint and by
cd-server's merge). A list of objects rather than a scalar so
passage-level full-text search (cd-platform#131) can add `{"via":
"text", "section", "excerpt", "distance"}` entries and a bill can carry
more than one reason, without reshaping the response. A client groups
exact matches above related ones on `meta.matches` rather than list
order. No `relationships` (a search bill points at nothing modelled), no
votes (that's the `/votes` endpoint's job -- cd-server merges the two by
resource id).

`filter[query]` (required, 1-500 chars) is a JSON:API filter on the bill
collection; `page[size]` (optional, 1-50, default 10) caps the result --
no offset/cursor pagination yet. The old `q` / `bioguide_id` / `limit`
params are gone; `JsonApiRoute` `400`s any undeclared query parameter. A
Bedrock failure is `503` (JSON:API error document, retryable).

## Running and testing

Part of cd-server: `make start-server` serves it at
`http://localhost:8000/v1/...`, and `make test-server` runs its tests
(`tests/api/`) alongside cd-server's own. See `README.md`.

```bash
curl 'http://localhost:8000/v1/members?filter[state]=GA&filter[district]=5'
```

`tests/api/test_transform.py` is pure unit tests with no dependencies.
`tests/api/test_app.py` and `tests/api/test_db.py` seed real rows in a
dedicated `congressional_app_test` database (`cd-platform#16`; `make
test-server` passes `CD_API_PGDATABASE=congressional_app_test`). That
database's schema is owned by cd-etl, so run `make test-etl` from the repo
root at least once first. Otherwise these tests fail with "relation does not
exist" rather than skipping, because the database exists but has no schema
yet. CI applies cd-etl's migrations there before `make test-server`.

## Releasing

Released with cd-server (`cd-server-v*` tags, see `README.md`). The OpenAPI
spec that `cd-website`'s docs viewer reads from a public S3 bucket
(`cd-platform-openapi-spec-<account-id>`, cd-infra#18) used to be published
by the Lambda-era `cd-api-deploy.yml` (now deleted, along with `cd-api/`). Moving that step into cd-server's
deploy workflow is a follow-up. Until then the published spec stays at its
last cd-api release.
