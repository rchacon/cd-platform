from pathlib import Path

import strawberry

VERSION_FILE = Path(__file__).parent / "VERSION"


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "dev"


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return _read_version()


schema = strawberry.Schema(query=Query)
