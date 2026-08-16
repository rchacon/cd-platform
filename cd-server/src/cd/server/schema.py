from pathlib import Path

import strawberry
from cd.lib.version import read_version

# Read once at import time, not per-request -- the VERSION file is baked
# into the image and never changes for the life of the process.
VERSION = read_version(Path(__file__).parent)


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return VERSION


schema = strawberry.Schema(query=Query)
