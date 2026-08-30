from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from cd.api.models import PROBLEM_DETAIL_SCHEMA, VALIDATION_PROBLEM_DETAIL_SCHEMA


def error_response(description: str, model_name: str) -> dict:
    """An OpenAPI `responses` entry pointing at a shared problem+json schema.

    Reused verbatim across every route's `responses=` so the documented
    error shape can't drift between routes.
    """
    return {
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": f"#/components/schemas/{model_name}"}
            }
        },
    }


def build_openapi(app: FastAPI) -> dict:
    """Assemble (and memoize on `app.openapi_schema`) cd-api's OpenAPI doc.

    Layered on top of FastAPI's own generation to register two things it
    can't derive from the routes:

    - `ProblemDetail`/`ValidationProblemDetail` as reusable
      `components.schemas` -- they're never a route's `response_model`,
      only referenced by hand-written `$ref`s via `error_response()`.
    - the `X-Api-Key` security scheme -- API Gateway enforces the key
      ahead of this application, so there's no route-level dependency for
      FastAPI to pick it up from. Applied globally via the top-level
      `security` key.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        servers=app.servers,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})

    schemas = components.setdefault("schemas", {})
    schemas["ProblemDetail"] = PROBLEM_DETAIL_SCHEMA
    schemas["ValidationProblemDetail"] = VALIDATION_PROBLEM_DETAIL_SCHEMA

    components["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Api-Key",
            "description": (
                "Required on every request. Enforced by API Gateway in "
                "front of this application, not by cd-api's own code."
            ),
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]

    app.openapi_schema = schema
    return app.openapi_schema
