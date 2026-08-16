from pathlib import Path

import strawberry
from cd.lib.version import read_version

PACKAGE_DIR = Path(__file__).parent


def _read_version() -> str:
    return read_version(PACKAGE_DIR)


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return _read_version()


schema = strawberry.Schema(query=Query)
