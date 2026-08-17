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

`src/cd/lib/models.py` -- Pydantic models `Member` and `MembersResponse`
only, moved here from `cd-api` because `cd-server` actually consumes
them: it validates/parses cd-api's real HTTP/Lambda responses against
the same model cd-api itself built them from
(`MembersResponse(**result)`), instead of trusting the JSON shape
blindly. `cd-api`'s own `VersionResponse`/`ProblemDetail`/
`ValidationProblemDetail` deliberately stayed in
`cd-api/src/cd/api/models.py` -- `cd-server` never touches them, and
`cd-lib` is for code that's actually shared, not everywhere cd-api's
own models happen to live. `cd-api` still owns *building* `Member`s
(`transform.py`'s row -> dict functions, `response_model=`/`responses=`
in `app.py`) -- only the `Member`/`MembersResponse` *definitions*
moved, not the logic that populates them.

`src/cd/lib/apportionment.py` -- `SEATS_PER_STATE` (2020 census
apportionment, 50 states plus DC/PR/VI/GU/AS/MP each with one at-large
seat) and `NON_VOTING_TERRITORIES` (which of those keys are a non-voting
Delegate/Resident Commissioner seat rather than a full voting
Representative). Moved here from `cd-api/src/cd/api/apportionment.py`
(which still owns `max_valid_district`/`is_valid_district`, the
validation logic built on top of the table -- only the data moved) once
`cd-server`'s `getStates` GraphQL field needed the same seat counts and
voting status cd-api was already using to validate `district` query
params, rather than a second hand-transcribed copy.

`cd-server`'s GraphQL `Representative` and `Senator` types are both
derived from the same `Member` via
`strawberry.experimental.pydantic.type` -- also carries each field's
`Field(description=...)` into the generated GraphQL schema for free.
`Senator` deliberately excludes `role` (every Senator's is always
"Senator", redundant with `getSenators` itself; `Representative` keeps
it, since that's cd-api's only way to distinguish an actual
Representative from a Delegate/Resident Commissioner within the House
chamber) -- the two GraphQL types abstract away that cd-api's own
`Member`/`current_members` don't actually separate senators and
representatives into different tables, only a `chamber` column does
(`cd-api/src/cd/api/transform.py`'s `group_representatives()`).

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
`sys.path` -- silently breaking the other one's imports. `cd-server` and
`cd-api` both already have this (their own `src/cd/__init__.py` was
removed when each adopted `cd-lib`); `cd-etl` doesn't depend on `cd-lib`
yet and still has its `cd/__init__.py` -- harmless as long as that stays
true, but it'd need the same removal the moment it adds `cd-lib` as a
dependency. `src/cd/lib/__init__.py` itself is a normal package -- only
the shared `cd` parent needs to stay namespace-only.

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
have `cd-lib`'s code in it.** `cd-server` (`cd-server/pyproject.toml`)
uses `editable = true`: its whole life happens inside a container whose
filesystem is stable between build and run, so an editable install
(really just a `.pth`-style reference back to `cd-lib`'s own source
directory, `COPY`'d into the image at a matching path -- see
`cd-server/docker/Dockerfile`) works fine, and additionally means editing
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
