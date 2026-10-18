from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from cd.api.models import VersionResponse
from cd.api.openapi import error_response

# cd-server's own VERSION file (cd/server/VERSION), not one of cd.api's:
# this package ships inside cd-server's image now, so it reports the version
# of the deployable unit. cd-server's Dockerfile writes it at build time
# (the pushed tag with its `cd-server-v` prefix stripped); absent in local
# dev and tests, where read_version() falls back to "dev".
VERSION_FILE = Path(__file__).parent.parent.parent / "server" / "VERSION"

router = APIRouter(tags=["system"])


def read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "dev"


@router.get(
    "/version",
    response_model=VersionResponse,
    responses={
        405: error_response("HTTP method not allowed for this path.", "ProblemDetail"),
        500: error_response("An unexpected error occurred.", "ProblemDetail"),
    },
)
def get_version() -> dict:
    return {"version": read_version()}
