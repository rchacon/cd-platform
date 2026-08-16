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
`sys.path` -- silently breaking the other one's imports. `cd-server`
already has this (its `src/cd/__init__.py` was removed when it adopted
`cd-lib`); `cd-api`/`cd-etl` don't depend on `cd-lib` yet and still have
theirs -- harmless as long as that stays true, but they'd need the same
removal the moment either one adds `cd-lib` as a dependency.
`src/cd/lib/__init__.py` itself is a normal package -- only the shared `cd`
parent needs to stay namespace-only.

## Consuming it

A component depends on `cd-lib` as a local, editable path dependency, not
a published package:

```toml
dependencies = ["cd-lib"]

[tool.uv.sources]
cd-lib = { path = "../cd-lib", editable = true }
```

`uv.lock` records this as a relative path (`../cd-lib`), so it resolves
the same way in CI and in Docker as it does on a real checkout. Each
component keeps its own independent `pyproject.toml`/`uv.lock` -- this is
a plain path dependency, not a `uv` workspace, so adding `cd-lib` to one
component doesn't merge its lockfile with anyone else's.

`pyproject.toml`'s own `version` field (`0.1.0`) is nominal, not a real
release marker -- unlike `cd-api`/`cd-etl`/`cd-server`, nothing ever
resolves `cd-lib` against that number (no registry, no `cd-lib-v*` tag, no
`check-tag-version.sh`). An editable path dependency always uses whatever
code is on disk at build time, so the version a consumer actually embeds
is implicitly the git commit it was built from. Don't bother bumping this
field on changes; it'd be cosmetic. Worth revisiting only if `cd-lib` ever
needs two simultaneously-deployed consumers to depend on genuinely
incompatible versions of the same function -- the scenario where a real
version and a compatibility policy would start to matter.

A component whose build runs in Docker needs `cd-lib` reachable from its
own build context, which means using the repo root as that context (not
the component's own directory) and explicitly `COPY`ing `cd-lib` in --
see `cd-server/docker/Dockerfile`'s own comment for the concrete shape of
that.

## Testing

```bash
cd cd-lib
uv sync
uv run pytest tests/ -v
```
