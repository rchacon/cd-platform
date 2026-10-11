"""Source-agnostic HTTP/concurrency helpers shared by every client and DAG.

Not specific to any one external system -- `build_session` backs both
api.congress.gov and senate.gov fetches, and `fetch_concurrently` is
also used for non-HTTP work (bills_etl's per-bill DB syncs).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def build_session(pool_maxsize: int) -> requests.Session:
    # Shared across all of a DAG's API calls (including any thread pool
    # doing concurrent detail fetches) so requests reuse pooled
    # connections instead of paying a fresh TCP+TLS handshake every
    # call, and transient failures (rate limits, 5xxs) retry with
    # backoff instead of failing the whole call on the first hiccup.
    # The underlying urllib3 connection pool is thread-safe, so one
    # shared Session is the standard pattern for this. Only GET is used
    # against any source. pool_maxsize is a caller-supplied parameter
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
