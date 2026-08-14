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

# Most bills settle down once their legislative activity ends (enacted,
# failed, vetoed) -- their policy_area/subjects/summary won't meaningfully
# change again. This schema has no bill-status field to detect that
# directly, so as a coarse stand-in, a bill isn't re-checked again until
# at least this many days have passed since its last successful sync --
# cuts the recurring daily API/DB-write volume against a known-fixed
# vocabulary of a few hundred bills (per house_votes_etl's own docstring)
# at the cost of up to this many days' staleness on a genuinely-still-
# active bill.
REFRESH_MIN_INTERVAL_DAYS = 7

# refresh_bills processes this many bills concurrently, each fetching via
# bills_common.sync_bill's own further 3-way fan-out per bill -- sized so
# the shared session's connection pool comfortably covers both levels at
# once.
REFRESH_BATCH_WORKERS = 5

_API_SESSION = congress_api.build_session(pool_maxsize=REFRESH_BATCH_WORKERS * 3)

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
        return congress_api.get_current_congress(POSTGRES_CONN_ID)

    @task
    def extract_known_bills(congress: int) -> list[dict[str, Any]]:
        # Only bills already in the table -- see this module's own
        # docstring for why this DAG doesn't discover new bills itself.
        # The synced_at cutoff is REFRESH_MIN_INTERVAL_DAYS's staleness
        # backoff (see that constant's own comment).
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        rows = hook.get_records(
            """
            SELECT bill_type, bill_number FROM bills
            WHERE congress = %s AND synced_at < NOW() - (%s * INTERVAL '1 day')
            """,
            parameters=(congress, REFRESH_MIN_INTERVAL_DAYS),
        )
        known_bills = [{"bill_type": row[0], "bill_number": row[1]} for row in rows]
        logger.info(
            "Found %d already-synced bills due for a refresh for the %dth Congress",
            len(known_bills), congress,
        )
        return known_bills

    @task
    def refresh_bills(known_bills: list[dict[str, Any]], congress: int) -> None:
        # Concurrent via congress_api.fetch_concurrently, unlike
        # house_votes_etl's resolve_bills (which stays sequential because
        # two votes in the same batch there can race to insert the *same*
        # not-yet-existing bill). Neither race applies here -- every row
        # in known_bills already exists -- so what's actually needed is a
        # separate connection per worker: sync_bill's own cursor/commit
        # calls aren't safe to share across threads on one psycopg2
        # connection, unlike the pure-HTTP concurrent fetches elsewhere in
        # this codebase (fetch_vote_details, fetch_member_votes).
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)

        def refresh_one(bill: dict[str, Any]) -> None:
            conn = hook.get_conn()
            try:
                bills_common.sync_bill(
                    _API_SESSION, conn, congress, bill["bill_type"], bill["bill_number"],
                )
            except Exception:
                # A DB-side failure would otherwise leave this worker's
                # own connection in an aborted-transaction state -- moot
                # for any later iteration on this same connection since
                # it's closed right below, but still needed so the
                # failure itself commits nothing partial.
                conn.rollback()
                raise
            finally:
                conn.close()

        refreshed = congress_api.fetch_concurrently(
            known_bills, refresh_one, max_workers=REFRESH_BATCH_WORKERS,
        )
        logger.info(
            "Refreshed %d of %d known bills (%d failed)",
            len(refreshed), len(known_bills), len(known_bills) - len(refreshed),
        )

    congress = get_current_congress()
    refresh_bills(extract_known_bills(congress), congress)


bills_etl()
