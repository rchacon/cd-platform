from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

# RFC 9457 "Problem Details for HTTP APIs".
MEDIA_TYPE = "application/problem+json"


def problem_response(
    status: int, title: str, detail: str | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, media_type=MEDIA_TYPE, content=body)
