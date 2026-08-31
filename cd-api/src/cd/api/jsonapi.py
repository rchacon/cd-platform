from __future__ import annotations

import http
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# cd-api's HTTP-layer JSON:API plumbing for the endpoints that speak it
# (GET /members/{bioguide_id}, GET /members/{bioguide_id}/votes): the
# `application/vnd.api+json` media type, JSON:API error documents, and
# the per-route strictness the spec requires of a compliant server. The
# wire *models* (Resource/Document/CollectionDocument) live in cd-lib's
# `cd.lib.jsonapi`; this module is cd-api-only.
#
# Adopted: the document/resource/relationship shapes, the media type,
# error documents, and 400/415/406 on malformed requests. NOT adopted
# (all optional): `included`/`?include=`, sparse fieldsets, pagination,
# `sort`, relationship link endpoints, the top-level `jsonapi` object.
# The bespoke endpoints (GET /members list, GET /version) keep RFC 9457
# problem+json -- they don't claim JSON:API.

MEDIA_TYPE = "application/vnd.api+json"


class JsonApiResponse(JSONResponse):
    # Starlette only appends "; charset=..." to text/* media types, so
    # this serialises as exactly `application/vnd.api+json` -- no
    # media-type parameters, as the spec requires of responses.
    media_type = MEDIA_TYPE


def _phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def error_document(
    status: int, detail: str | None = None, *, source: dict[str, str] | None = None
) -> dict[str, Any]:
    """A JSON:API error document: ``{"errors": [{"status", "title", ...}]}``."""
    error: dict[str, Any] = {"status": str(status), "title": _phrase(status)}
    if detail:
        error["detail"] = detail
    if source:
        error["source"] = source
    return {"errors": [error]}


def error_response(
    status: int, detail: str | None = None, *, source: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        error_document(status, detail, source=source),
        status_code=status,
        media_type=MEDIA_TYPE,
    )


def _source_for(loc: list[Any]) -> dict[str, str] | None:
    if not loc:
        return None
    origin = loc[0]
    if origin in ("query", "path"):
        return {"parameter": str(loc[-1])}
    if origin == "header":
        return {"header": str(loc[-1])}
    if origin == "body":
        return {"pointer": "/" + "/".join(str(p) for p in loc[1:])}
    return None


def validation_error_response(exc: RequestValidationError) -> JSONResponse:
    """Map FastAPI's per-field validation errors to a JSON:API error
    document -- one error object each, `source.parameter`/`pointer` set
    from the field's location."""
    errors: list[dict[str, Any]] = []
    for err in exc.errors():
        obj: dict[str, Any] = {
            "status": "422",
            "title": _phrase(422),
            "detail": err.get("msg", "Invalid request."),
        }
        source = _source_for(list(err.get("loc", [])))
        if source:
            obj["source"] = source
        errors.append(obj)
    return JSONResponse(
        {"errors": errors or [{"status": "422", "title": _phrase(422)}]},
        status_code=422,
        media_type=MEDIA_TYPE,
    )


# JSON:API 1.1 exempts two media-type parameters: `profile` (a profile
# request -- we support none, so we ignore it) and `ext` (an extension --
# we support none, so any `ext` value IS an unsupported extension and
# must be rejected). `q` is an RFC 9110 quality weight, not a media-type
# parameter at all, so it never counts as "modifying" the media type.
_EXEMPT_MEDIA_TYPE_PARAMS = frozenset({"profile"})


def _split_unquoted(value: str, sep: str) -> list[str]:
    """Split `value` on `sep`, ignoring occurrences inside a
    double-quoted run -- RFC 9110 lets a parameter value be a quoted
    string, so `profile="https://x/a;b"` is one parameter, and an Accept
    value like `...; profile="a,b"` is one media-type instance."""
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in value:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _media_type(instance: str) -> str:
    return _split_unquoted(instance, ";")[0].strip().lower()


def _offending_media_type_params(instance: str) -> list[str]:
    """The media-type parameter names on `instance` that disqualify it --
    i.e. not `q` (a weight) and not `profile` (1.1-exempt). `ext` is
    included: we support no extensions."""
    return [
        name
        for part in _split_unquoted(instance, ";")[1:]
        if (name := part.split("=", 1)[0].strip().lower())
        and name != "q"
        and name not in _EXEMPT_MEDIA_TYPE_PARAMS
    ]


def _reject_parametrized_media_type(request: Request) -> JSONResponse | None:
    # JSON:API 1.1: a server MUST 415 when the request's Content-Type is
    # the JSON:API media type modified by a media-type parameter other
    # than `ext`/`profile`, and MUST 406 when every JSON:API instance in
    # Accept is so modified.
    content_type = request.headers.get("content-type", "")
    if _media_type(content_type) == MEDIA_TYPE and _offending_media_type_params(
        content_type
    ):
        return error_response(
            415,
            "The JSON:API media type must be sent without media-type "
            "parameters (other than 'profile').",
        )
    accept = request.headers.get("accept", "")
    jsonapi_instances = [
        part.strip()
        for part in _split_unquoted(accept, ",")
        if _media_type(part) == MEDIA_TYPE
    ]
    if jsonapi_instances and all(
        _offending_media_type_params(part) for part in jsonapi_instances
    ):
        return error_response(
            406, "The JSON:API media type must be accepted without media-type parameters."
        )
    return None


class JsonApiRoute(APIRoute):
    """APIRoute for JSON:API endpoints.

    On top of the normal handler it: rejects a parametrized JSON:API
    media type (`415`/`406`); rejects any query parameter the route
    doesn't declare, or declares but that appears more than once (`400`)
    -- the former covers unsupported spec parameters like `include`,
    `sort`, `fields[...]`, `page[...]`; the latter stops a repeated
    `?filter[bill]=a&filter[bill]=b` from binding only one occurrence and
    silently dropping the rest. It also renders `HTTPException`,
    request-validation, and unhandled failures as JSON:API error
    documents rather than letting them reach the app's problem+json
    handlers. (Routing-layer `404`/`405` on a JSON:API path never reach
    here -- `app.py` formats those.)
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        # The query-param keys this route declares (aliases -- e.g.
        # "filter[bill]"). Anything else in the query string is rejected.
        # These routes have no sub-dependencies, so the route's own
        # `dependant.query_params` is the complete set.
        allowed = {field.alias for field in self.dependant.query_params}

        async def handler(request: Request) -> Response:
            media_type_error = _reject_parametrized_media_type(request)
            if media_type_error is not None:
                return media_type_error

            seen: dict[str, int] = {}
            for key, _ in request.query_params.multi_items():
                seen[key] = seen.get(key, 0) + 1
            unsupported = sorted(k for k in seen if k not in allowed)
            if unsupported:
                return error_response(
                    400,
                    "Unsupported query parameter(s): " + ", ".join(unsupported) + ".",
                )
            repeated = sorted(k for k, n in seen.items() if n > 1)
            if repeated:
                return error_response(
                    400,
                    "Query parameter(s) given more than once: "
                    + ", ".join(repeated)
                    + " (use a comma-separated value).",
                )

            try:
                return await original(request)
            except RequestValidationError as exc:
                return validation_error_response(exc)
            except StarletteHTTPException as exc:
                return error_response(exc.status_code, exc.detail)
            except Exception:
                # Mirrors app.py's unhandled_exception_handler (which
                # never sees these -- they're caught here first) so a JSON:API
                # route's 500 is still a JSON:API document, still logged.
                logger.exception(
                    "Unhandled exception for %s %s", request.method, request.url.path
                )
                return error_response(500, "An unexpected error occurred.")

        return handler
