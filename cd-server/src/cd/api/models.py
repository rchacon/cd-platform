from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class VersionResponse(BaseModel):
    version: str


class ProblemDetail(BaseModel):
    """RFC 9457 "Problem Details for HTTP APIs"."""

    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int
    detail: str | None = None


class ValidationProblemDetail(ProblemDetail):
    errors: list[dict[str, Any]]


# Computed once and reused verbatim across every route's `responses=`
# declaration in app.py, so the documented error shape can't drift between
# routes and doesn't get recomputed per route.
PROBLEM_DETAIL_SCHEMA = ProblemDetail.model_json_schema()
VALIDATION_PROBLEM_DETAIL_SCHEMA = ValidationProblemDetail.model_json_schema()
