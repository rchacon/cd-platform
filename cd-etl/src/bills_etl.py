"""Refreshes already-synced bills' policy_area/subjects/title/CRS summary.

Resolves rchacon/cd-platform#52: house_votes_etl's get_or_sync_bill() is
sync-once -- once a bill is stored, nothing re-fetches it, even though a
bill's policy_area can be reassigned and its legislativeSubjects can be
added or removed over its lifetime. This DAG is that missing refresh
path, on its own daily schedule.

Deliberately refresh-only, not a discovery DAG: it only re-syncs bills
already present in the bills table (via extract_known_bills), the same
"only sync bills something actually references" precedent
house_votes_etl's own module docstring already established (of 18,140
bills in the 119th Congress, only a few hundred are ever referenced by a
vote -- proactively discovering and syncing every bill in a Congress via
a new /bill/{congress} list call would reintroduce that exact
wasted-API-call problem). New-bill discovery stays where it already is:
house_votes_etl's (and, later, senate_votes_etl's) on-demand
get_or_sync_bill() path.

Deliberately not triggered by or triggering house_votes_etl/
senate_votes_etl, and not scheduled to run before them. Once discovery
and refresh are split this way, a vote-sync DAG no longer depends on
this one having just run: it still does its own on-demand fetch for any
bill not yet known, and staleness of an already-known bill's
policy_area/subjects is a downstream reader's problem (cd-api/
cd-lookup), not a vote-sync correctness problem. Both DAGs share the
same fetch+upsert logic via bills_common.sync_bill().
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import bills_common
import congress_api
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

POSTGRES_CONN_ID = "congressional_postgres"

# bills_common.sync_bill fetches a bill's detail/subjects/summaries
# concurrently (3 requests per bill) -- matches that fan-out, same as
# house_votes_etl sizing its own session to MEMBER_VOTES_FETCH_WORKERS.
REFRESH_FETCH_WORKERS = 3

_API_SESSION = congress_api.build_session(pool_maxsize=REFRESH_FETCH_WORKERS)

logger = logging.getLogger(__name__)


@dag(
    dag_id="bills_etl",
    description="Refresh already-synced bills' policy_area/subjects/title/CRS summary",
    schedule="@daily",
    start_date=datetime(2025, 1, 3),
    catchup=False,
    default_args={"retries": 2},
    tags=["congress"],
)
def bills_etl():

    @task
    def get_current_congress() -> int:
        # Same query members_etl.py's/house_votes_etl.py's task of the
        # same name uses -- Postgres's own current_congress() function is
        # the single place every ETL agrees on "which Congress is
        # current."
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        row = hook.get_first("SELECT current_congress()")
        if row is None or row[0] is None:
            raise ValueError("No current congress found in congresses table")
        return row[0]

    @task
    def extract_known_bills(congress: int) -> list[dict[str, Any]]:
        # Only bills already in the table -- see this module's own
        # docstring for why this DAG doesn't discover new bills itself.
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        rows = hook.get_records(
            "SELECT bill_type, bill_number FROM bills WHERE congress = %s",
            parameters=(congress,),
        )
        known_bills = [{"bill_type": row[0], "bill_number": row[1]} for row in rows]
        logger.info(
            "Found %d already-synced bills to refresh for the %dth Congress",
            len(known_bills), congress,
        )
        return known_bills

    @task
    def refresh_bills(known_bills: list[dict[str, Any]], congress: int) -> None:
        # Sequential, not congress_api.fetch_concurrently: each bill's
        # own sync_bill call already commits independently (see
        # bills_common.sync_bill), and unlike house_votes_etl's
        # resolve_bills, there's no shared not-yet-existing row two
        # entries in this batch could race to insert -- every row here
        # already exists. Sequential is simply the simplest thing that
        # works at the volume this refresh set actually reaches (a few
        # hundred bills, per house_votes_etl's own docstring).
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()

        refreshed_count = 0
        failed_count = 0
        try:
            for bill in known_bills:
                try:
                    bills_common.sync_bill(
                        _API_SESSION, conn, congress, bill["bill_type"], bill["bill_number"],
                    )
                    refreshed_count += 1
                except Exception as exc:
                    # Broad on purpose, same rationale as resolve_bills:
                    # one bill's failure (HTTP error, validation error,
                    # DB error) shouldn't abort refreshing the rest.
                    # conn.rollback() is required -- a DB-side failure
                    # leaves the shared connection in an
                    # aborted-transaction state that would otherwise
                    # break every subsequent iteration's queries too.
                    conn.rollback()
                    failed_count += 1
                    logger.error(
                        "Failed to refresh bill %s %d: %s",
                        bill["bill_type"], bill["bill_number"], exc,
                    )
        finally:
            conn.close()

        logger.info(
            "Refreshed %d of %d known bills (%d failed)",
            refreshed_count, len(known_bills), failed_count,
        )

    congress = get_current_congress()
    refresh_bills(extract_known_bills(congress), congress)


bills_etl()
