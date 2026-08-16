import os
from pathlib import Path

import strawberry
from cd.lib.version import read_version
from strawberry.extensions import DisableIntrospection

# Read once at import time, not per-request -- the VERSION file is baked
# into the image and never changes for the life of the process.
VERSION = read_version(Path(__file__).parent)

# Same flag app.py uses to gate the GraphiQL IDE -- introspection is what
# powers GraphiQL's own schema explorer/autocomplete, so the two are
# enabled and disabled together. Leaving introspection on while only
# hiding the IDE would still let any POST client walk the full schema.
GRAPHIQL_ENABLED = os.environ.get("GRAPHIQL_ENABLED", "false").lower() == "true"


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return VERSION


schema = strawberry.Schema(
    query=Query,
    extensions=[] if GRAPHIQL_ENABLED else [DisableIntrospection],
)
