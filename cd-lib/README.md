# CD-Lib

Shared code for `cd-platform`'s Python services (`cd-api`, `cd-etl`,
`cd-server`) -- not deployed on its own, only consumed by the other
components as a local path dependency.

## What it does

`src/cd/lib/version.py` -- `read_version(package_dir)` reads a component's
own `VERSION` file (written into its image/zip at release time, never
committed) and falls back to `"dev"` if it's missing. Each component
passes its own `Path(__file__).parent`, not this package's -- the
`VERSION` file lives alongside the *consuming* component's deployed
package, not alongside `cd-lib`.

`src/cd/lib/models.py` -- Pydantic model `Member`, consumed only by
`cd-server`: it flattens cd-api's JSON:API `/members` collection
(`CollectionDocument[MemberDetail]`, see below) into a `list[Member]`,
one `member` resource per `Member` (resource `id` -> the `bioguide_id`
field), and derives its GraphQL `Representative`/`Senator` types from it.
`cd-api` itself builds `MemberDetail`, not this -- `Member` is cd-server's
own flattened shape and the only reason it stays a separate model.
(Earlier it also parsed cd-api's pre-JSON:API bespoke
`{senators, representatives}` body via a `MembersResponse` model;
that body and model are both gone as of cd-platform#104 PR B /
cd-api-v0.3.8.) `cd-api`'s own `VersionResponse`/`ProblemDetail`/
`ValidationProblemDetail` deliberately stayed in
`cd-api/src/cd/api/models.py` -- `cd-server` never touches them, and
`cd-lib` is for code that's actually shared, not everywhere cd-api's
own models happen to live.

These models are deliberately **lenient** (Pydantic's default
`extra="ignore"`, no `extra="forbid"`): they're a shared contract, and
`cd-server` bundles its own independently-versioned copy of `cd-lib`, so
a field `cd-api` adds to a response must not break a `cd-server` that
hasn't picked up the new `cd-lib` yet -- the unknown field is just
dropped consumer-side until it does. `cd-api`'s producer-side guard
against its own response shape drifting from the OpenAPI spec lives in
its tests (`test_openapi_*_documents_*_fields` plus the `transform.py`/
`search.py` shaper unit tests, which assert the exact field set), not in
the shared model.

`src/cd/lib/models.py` also has `Bill`, the `attributes` payload of each
`bill` resource in `cd-api`'s `GET /bills`
(`CollectionDocument[Bill]`, document `meta: {query}`) -- `cd-platform#9`'s
semantic search over bills. Placed here rather than in `cd-api`-local
models for the same reason as `Member`: the future `cd-server` resolver
validates against the exact shape `cd-api` builds.
Only intrinsic bill data: the canonical bill id (`bills.bill_key`) is
the resource `id`, not a field; why the bill matched *this* search is
the resource's `meta.matches` (a list of `{"via": "policy_area" |
"subject" | "summary" | ...}`), **not** an attribute -- it's meaningless
outside one search response, so it must not ride on the model cd-server
merges by id. No `votes` -- that's `GET /members/{bioguide_id}/votes`'
`RollCallVote`; cd-server merges the two by resource id.

`MemberDetail` and `RollCallVote` are the `attributes` payloads for
`cd-api`'s member-resource endpoints, which return a JSON:API document
(see `jsonapi.py` below): `MemberDetail` backs both `GET /members` (the
list -- `CollectionDocument[MemberDetail]`) and
`GET /members/{bioguide_id}` (`Document[MemberDetail]`), `RollCallVote`
backs `GET /members/{bioguide_id}/votes`. `MemberDetail` is the person fields
*minus* `bioguide_id` (the id is the resource `id`), plus required
`state` and `in_office`; it deliberately does **not** extend `Member`,
since `Member` is cd-server's flattened shape and keeps `bioguide_id`
in-body (its GraphQL types derive from it) -- the two share their field
*descriptions* via module constants instead. `RollCallVote` is the attributes of a
`roll_call_vote` resource (one member's cast position in one roll
call): `vote_cast` plus `vote_question`/`result`/`vote_date`
denormalised from the roll call -- the bill and member it relates to
are in the resource's `relationships`, not here.

`src/cd/lib/jsonapi.py` -- `Resource[A]` / `Document[A]` /
`CollectionDocument[A]` (plus `ResourceIdentifier` and `Relationship`
for linkage), generic Pydantic wrappers for cd-api's JSON:API document
and resource shapes: the document envelope (`data` holding a resource or
list), typed `attributes`, `relationships` carrying resource *linkage*
(`{type, id}` pointers), and per-resource `meta` for anything about
*this response* rather than the resource itself (a `bill`'s search-tier
`match`). These are the wire *models* only --
the HTTP layer (the `application/vnd.api+json` media type, JSON:API
error documents, request-side strictness) lives in `cd-api`'s own
`cd.api.jsonapi`. What's deliberately not built anywhere is the optional
machinery: no `included`/`?include=`, no sparse fieldsets, no
relationship `links` or their endpoints, no pagination/`sort`, no
top-level `jsonapi` object -- the one caller (`cd-server`) runs a fixed
two-call merge and needs none of it. Each endpoint parameterises the
wrappers with its own attributes model (`Document[MemberDetail]`,
`CollectionDocument[RollCallVote]`), which FastAPI renders into the
OpenAPI schema. Only `cd-api`'s resource endpoints use it so far;
`GET /members` (list) and `GET /version` keep
their bespoke shapes.

`src/cd/lib/apportionment.py` -- `SEATS_PER_STATE` (2020 census
apportionment, 50 states plus DC/PR/VI/GU/AS/MP each with one at-large
seat) and `NON_VOTING_TERRITORIES` (which of those keys are a non-voting
Delegate/Resident Commissioner seat rather than a full voting
Representative). Moved here from `cd-api/src/cd/api/apportionment.py` (only the data --
`max_valid_district`/`is_valid_district`, built on the table, live in
`cd-api/src/cd/api/routes/members.py`, that endpoint being their only
caller) once `cd-server`'s `getStates` GraphQL field needed the same
seat counts and voting status cd-api was already using to validate
`district` query params, rather than a second hand-transcribed copy.
Also `normalize_district(state, district)` -- maps the U.S. Census
Bureau's FIPS "nonvoting delegate" code `98` to this project's at-large
`0`, scoped to `NON_VOTING_TERRITORIES`. Unlike `is_valid_district` this
one *is* shared: `cd-server` runs it at its geocoder boundary and
`cd-api` on `GET /members`' `filter[district]`, both so a caller that
geocodes for itself (e.g. `cd-lookup`) never has to translate the code
(cd-platform#72).

`src/cd/lib/bedrock.py` -- `build_bedrock_client(config=None)` and
`embed(client, text)` for Amazon Titan Text Embeddings V2. Shared by
`cd-api`'s `GET /bills` (embeds a query at request time) and
`cd-etl`'s `bills_common.sync_bill` (embeds a bill's title + CRS
summary). This move is what first made `cd-etl` a `cd-lib` consumer.
IAM/task-role auth via boto3's default credential chain -- no API key.
`build_bedrock_client` takes an optional `botocore.config.Config` so a
caller can bound the call's worst case (cd-api passes one: its Lambda's
25s function timeout is well under botocore's 60s connect/read
defaults). `cd-lib` gains `boto3` as a dependency here -- every consumer
already had it.

`cd-server`'s GraphQL `Representative` and `Senator` types are both
derived from the same `Member` via
`strawberry.experimental.pydantic.type` -- also carries each field's
`Field(description=...)` into the generated GraphQL schema for free.
`Senator` deliberately excludes `role` (every Senator's is always
"Senator", redundant with `getSenators` itself; `Representative` keeps
it, since that's cd-api's only way to distinguish an actual
Representative from a Delegate/Resident Commissioner within the House
chamber) and `district` (always `null` for a Senator -- senators
represent the whole state, not a district; `Representative` keeps it,
since that's the whole point there) -- the two GraphQL types abstract
away that cd-api's own `current_members` doesn't actually separate
senators and representatives into different tables, only a `chamber`
column does (cd-server sends `filter[chamber]` and splits the flat
`/members` collection back out in `cd_api_service._members`).

`cd-lib` uses the same `src/cd/lib/` layout as `cd-api`/`cd-etl`/`cd-server`'s
own `src/cd/<component>/`, unlike those three, `cd-lib` genuinely gets
*installed* (it declares `[build-system]`/hatchling and is pulled in as an
editable dependency) rather than just run in place, so it never needed
`src/` to support a `pythonpath` pytest workaround the way they do -- it's
here for structural consistency with its siblings, not because it was
technically required.

## Why any consumer needs no `cd/__init__.py` of its own

`cd-api`/`cd-etl`/`cd-server` each own their own top-level `cd` package
(`cd.api`, `cd.etl`, `cd.server`). Any of them that depends on `cd-lib`
must have no `cd/__init__.py` of its own -- `cd` needs to be an implicit
(PEP 420) namespace package there, not a regular one, so that component's
own `cd.<component>` and `cd.lib` end up importable from the same `cd`
namespace, merged from two physically separate locations (the consumer's
own `src/`, and wherever `cd-lib` gets installed). A real `cd/__init__.py`
would make that directory a regular package instead, and Python would only
ever see whichever one of the two `cd` directories came first on
`sys.path` -- silently breaking the other one's imports. `cd-server`, `cd-api`, and
`cd-etl` all have this -- each removed its own `src/cd/__init__.py` when
it adopted `cd-lib`. `src/cd/lib/__init__.py` itself is a normal package
-- only the shared `cd` parent needs to stay namespace-only.

## Consuming it

A component depends on `cd-lib` as a local path dependency, not a
published package:

```toml
dependencies = ["cd-lib"]

[tool.uv.sources]
cd-lib = { path = "../cd-lib" }
```

`uv.lock` records this as a relative path (`directory = "../cd-lib"`), so
it resolves the same way in CI, Docker, and a Lambda zip build as it does
on a real checkout. Each component keeps its own independent
`pyproject.toml`/`uv.lock` -- this is a plain path dependency, not a `uv`
workspace, so adding `cd-lib` to one component doesn't merge its
lockfile with anyone else's.

**`editable = true` is a real, load-bearing choice, not a style
preference -- get it wrong and the deployed artifact silently doesn't
have `cd-lib`'s code in it.** `cd-server` and `cd-etl` use
`editable = true`: both live entirely inside a container whose
filesystem is stable between build and run, so an editable install
(really just a `.pth`-style reference back to `cd-lib`'s own source
directory, `COPY`'d into the image at a matching path -- see
`cd-server/docker/Dockerfile` and `cd-etl/docker/Dockerfile`) works
fine, and additionally means editing
`cd-lib` locally shows up immediately via the bind mount, no rebuild
needed. `cd-api` deliberately does **not** use `editable = true`: its
deploy path (`uv export` + `uv pip install --target package`, see
`cd-api/README.md`'s Releasing section) produces a zip that's the *only*
thing that ships to Lambda -- there is no persistent source tree
alongside it at runtime the way a container has. Confirmed empirically:
with `editable = true`, `uv pip install --target` produced only a
dangling `.pth` file pointing at this dev machine's own absolute
filesystem path (`/home/.../cd-lib/src`) and no `cd/lib/` directory at
all in the target -- the zip would deploy successfully and then fail at
import time in Lambda, since that path doesn't exist there. Without
`editable = true`, the same command produces real copied files
(`package/cd/lib/version.py`, `models.py`, `__init__.py`), which is what
actually needs to happen for a self-contained deploy artifact.

**Local-dev gotcha, non-editable consumers only**: a non-editable
`cd-lib` install is a real, static copy taken at `uv sync` time -- a
plain `uv sync` in a consumer that already has a populated `.venv`
does *not* reliably notice that `cd-lib`'s own source changed and
rebuild it (confirmed empirically while renaming a model: `cd-api`'s
existing `.venv` kept serving the old class name until
`uv sync --reinstall-package cd-lib` forced a rebuild). A CI run isn't
at risk of this -- `actions/checkout` + `uv sync --locked` always
starts from a fresh `.venv` reflecting whatever was just checked out --
but a local edit-`cd-lib`-then-test-`cd-api` loop can silently run
against stale code without it. `cd-server`'s `editable = true` install
doesn't have this problem, since it always resolves through the
bind-mounted source directory rather than a point-in-time copy.

`pyproject.toml`'s own `version` field (`0.1.0`) is nominal, not a real
release marker -- unlike `cd-api`/`cd-etl`/`cd-server`, nothing ever
resolves `cd-lib` against that number (no registry, no `cd-lib-v*` tag, no
`check-tag-version.sh`). Either way -- editable or not -- a consumer's
`uv.lock` just points at the local directory; there's no real "pinned
version" to speak of, so don't bother bumping this field on changes, it'd
be cosmetic. Worth revisiting only if `cd-lib` ever needs two
simultaneously-deployed consumers to depend on genuinely incompatible
versions of the same function -- the scenario where a real version and a
compatibility policy would start to matter.

A component whose build runs in Docker needs `cd-lib` reachable from its
own build context, which means using the repo root as that context (not
the component's own directory) and explicitly `COPY`ing `cd-lib` in --
see `cd-server/docker/Dockerfile`'s own comment for the concrete shape of
that. A component that deploys as a Lambda zip instead just needs
`actions/checkout@v4` to have checked out the whole repo (so `../cd-lib`
is a real sibling directory in CI, same as on a local checkout) -- no
Dockerfile/COPY step involved at all.

## Testing

```bash
cd cd-lib
uv sync
uv run pytest tests/ -v
```
