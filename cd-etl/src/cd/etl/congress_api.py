from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import requests
from airflow.providers.postgres.hooks.postgres import PostgresHook
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


def build_session(pool_maxsize: int) -> requests.Session:
    # Shared across all of a DAG's API calls (including any thread pool
    # doing concurrent detail fetches) so requests reuse pooled
    # connections instead of paying a fresh TCP+TLS handshake every
    # call, and transient failures (rate limits, 5xxs) retry with
    # backoff instead of failing the whole call on the first hiccup.
    # The underlying urllib3 connection pool is thread-safe, so one
    # shared Session is the standard pattern for this. Only GET is used
    # against this API. pool_maxsize is a caller-supplied parameter
    # (not a shared default) since it should match that DAG's own
    # concurrency level -- e.g. members_etl.py's DETAIL_FETCH_WORKERS.
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset(["GET"]),
            ),
            pool_maxsize=pool_maxsize,
        ),
    )
    return session


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


def fetch_concurrently(
    ids: list[Any], fetch_one: Callable[[Any], Any], max_workers: int,
) -> list[Any]:
    # Fetches one result per id concurrently. `ids` need only be
    # hashable -- a tuple key (e.g. (session, roll_call_number)) works
    # as well as a plain string id. A single id's failure (404, rate
    # limit, transient 5xx, or a validation error if fetch_one parses
    # its result) is logged and skipped rather than discarding every
    # other already-fetched result.
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, id_): id_ for id_ in ids}
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", futures[future], exc)

    return results


class IsolatedTransaction:
    """Runs one unit of work against its own fresh Postgres connection,
    isolating that unit's failure from the rest of a run: commits on a
    clean exit, rolls back + logs + suppresses the exception on failure
    (the caller's `with` block just ends early, no exception propagates),
    and always closes the connection.

    A fresh connection per unit rather than one long-lived connection
    reused across many units means a connection is never left open (and
    vulnerable to an infra-level idle-connection timeout) across an
    unrelated network-bound phase -- e.g. an API fetch -- between units
    of work; it's only ever open for as long as this one unit's actual DB
    work takes. `rollback()` on an already-dead connection is itself
    swallowed rather than allowed to escape uncaught, so one unit's
    connection trouble can't abort whatever's driving the loop this runs
    inside of.

    Check `.failed` after the `with` block to tell whether this unit
    succeeded, e.g. for a running failure count:

        for item in items:
            txn = IsolatedTransaction(hook, f"item {item!r}")
            with txn as conn:
                ...  # do this item's DB work against conn
            if txn.failed:
                failed_count += 1

    Not used by bills_common.sync_bill()/bills_etl.refresh_bills's
    refresh_one(), even though both do a similar isolate-and-continue
    dance -- sync_bill() commits multiple times internally rather than
    once at the end, and refresh_one() re-raises into
    fetch_concurrently()'s own per-item isolation instead of handling it
    itself, so neither actually fits this shape without a larger,
    separate refactor of their own control flow.
    """

    def __init__(self, hook: PostgresHook, description: str):
        self._hook = hook
        self._description = description
        self.failed = False

    def __enter__(self) -> Any:
        self._conn = self._hook.get_conn()
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass  # connection's already dead -- nothing left to roll back
            self.failed = True
            logger.error("Failed to sync %s: %s", self._description, exc)
        self._conn.close()
        return True  # suppress -- caller's loop continues to the next unit


def get_current_congress(postgres_conn_id: str) -> int:
    # Postgres's own current_congress() function is the single place
    # every ETL agrees on "which Congress is current." Shared here since
    # house_votes_etl.py and bills_etl.py both need this exact,
    # no-upstream-dependency lookup as their very first task -- their
    # copies were identical. members_etl.py's own get_current_congress
    # keeps its own copy rather than being forced onto this shape: it
    # takes a dummy upstream-ordering argument (so Airflow sequences it
    # after sync_current_congress) that this shared, zero-arg version
    # has no equivalent for.
    hook = PostgresHook(postgres_conn_id=postgres_conn_id)
    row = hook.get_first("SELECT current_congress()")
    if row is None or row[0] is None:
        raise ValueError("No current congress found in congresses table")
    return row[0]


def source_hash(*parts: Any) -> str:
    normalized = "|".join(
        str(part).strip().lower() if part is not None else "" for part in parts
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
