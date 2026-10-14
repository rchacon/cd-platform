from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from cd.api.jsonapi import MEDIA_TYPE as JSONAPI_MEDIA_TYPE
from cd.api.models import PROBLEM_DETAIL_SCHEMA, VALIDATION_PROBLEM_DETAIL_SCHEMA

# The JSON:API error document (`{"errors": [{...}]}`) returned by the
# JSON:API resource routes -- the counterpart to ProblemDetail for the
# bespoke ones. Registered by hand (like the problem+json schemas)
# because it's never a route's `response_model`, only a `$ref` target
# from `jsonapi_error_response()`.
JSONAPI_ERROR_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "HTTP status code, as a string.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable summary, stable across occurrences.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Human-readable explanation specific to this occurrence.",
                    },
                    "source": {
                        "type": "object",
                        "description": "What in the request caused the error.",
                        "properties": {
                            "parameter": {"type": "string"},
                            "pointer": {"type": "string"},
                            "header": {"type": "string"},
                        },
                    },
                },
                "required": ["status", "title"],
            },
        }
    },
    "required": ["errors"],
}


def error_response(description: str, model_name: str) -> dict:
    """An OpenAPI `responses` entry pointing at a shared problem+json schema.

    Reused verbatim across every bespoke route's `responses=` so the
    documented error shape can't drift between routes.
    """
    return {
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": f"#/components/schemas/{model_name}"}
            }
        },
    }


def jsonapi_error_response(description: str) -> dict:
    """An OpenAPI `responses` entry for a JSON:API route's error -- points
    at the shared `JsonApiErrorDocument` schema, served as
    `application/vnd.api+json`."""
    return {
        "description": description,
        "content": {
            JSONAPI_MEDIA_TYPE: {
                "schema": {"$ref": "#/components/schemas/JsonApiErrorDocument"}
            }
        },
    }


def build_openapi(app: FastAPI) -> dict:
    """Assemble (and memoize on `app.openapi_schema`) cd-api's OpenAPI doc.

    Layered on top of FastAPI's own generation to register things it
    can't derive from the routes:

    - `ProblemDetail`/`ValidationProblemDetail`/`JsonApiErrorDocument` as
      reusable `components.schemas` -- never a route's `response_model`,
      only referenced by hand-written `$ref`s via `error_response()` /
      `jsonapi_error_response()`.
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
    schemas["JsonApiErrorDocument"] = JSONAPI_ERROR_DOCUMENT_SCHEMA

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
