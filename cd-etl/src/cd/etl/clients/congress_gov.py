from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, TypeVar

import requests
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def _congress_api_key() -> str:
    # Read lazily, on every actual call, rather than once at module
    # import time -- every DAG file imports this module transitively, so
    # an import-time read couples "can this module even be imported" to
    # "is CONGRESS_API_KEY configured," which broke dag-processor (which
    # parses DAG files but never calls the Congress API itself under
    # cd-infra's ECS decomposition, cd-infra#41) even though it never
    # needed the key at all (cd-platform#79). Only the code path that
    # actually makes a request pays this cost, and only fails here, not
    # at parse time.
    return os.environ["CONGRESS_API_KEY"]


def api_get(
    session: requests.Session, url: str, params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # api_key travels as a header, not a query param -- requests embeds
    # the full request URL (query string included) in an HTTPError's own
    # message, so a query-param key would leak into any log line or
    # Airflow task traceback that stringifies a failed request. Verified
    # live that api.congress.gov accepts X-Api-Key the same as api_key.
    response = session.get(
        url,
        params={**(params or {}), "format": "json"},
        headers={"X-Api-Key": _congress_api_key()},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def api_get_model(
    session: requests.Session,
    url: str,
    model: type[ModelT],
    params: dict[str, Any] | None = None,
) -> ModelT:
    # Validates the raw response into `model` in one step, so a
    # malformed/missing field raises one clear pydantic ValidationError
    # right here at the API boundary, instead of an obscure
    # KeyError/TypeError several functions later. Schema-agnostic on
    # purpose -- callers supply whichever model matches the endpoint
    # they're calling; this module doesn't know about any DAG's
    # specific response shapes.
    return model.model_validate(api_get(session, url, params))


def paginate(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    items_key: str,
    page_limit: int,
) -> Iterator[dict[str, Any]]:
    # Pages through a Congress.gov list endpoint's limit/offset
    # pagination, yielding each page's raw items until a short page
    # signals the end. Returns raw dicts rather than parsed models --
    # callers validate each item into whatever model fits their
    # endpoint's item shape.
    offset = 0
    while True:
        page = api_get(session, url, {**params, "limit": page_limit, "offset": offset})
        items = page.get(items_key, [])
        if not items:
            return

        yield from items

        if len(items) < page_limit:
            return
        offset += page_limit
