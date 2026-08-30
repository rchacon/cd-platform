from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from cd.api.models import VersionResponse
from cd.api.openapi import error_response

# cd-api-deploy.yml writes this into the Lambda zip (the pushed tag with
# its `cd-api-v` prefix stripped, alongside app.py); absent in local dev
# and tests, where read_version() falls back to "dev".
VERSION_FILE = Path(__file__).parent.parent / "VERSION"

router = APIRouter(tags=["meta"])


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
