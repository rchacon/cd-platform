"""Shared bill fetch+upsert logic, used both on-demand and on a schedule.

sync_bill() always fetches from the API and writes -- it has no
cache-check of its own. That makes it reusable two ways:

  - house_votes_etl.get_or_sync_bill() calls it only on a cache miss (a
    bill never seen before), keeping house_votes_etl's on-demand,
    sync-once behavior for new bills.
  - bills_etl calls it unconditionally for every bill already in the
    table, on its own schedule -- the refresh path rchacon/cd-platform#52
    asked for, since a bill's policy_area/subjects/CRS summary aren't
    fixed at introduction.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

import congress_api
import requests
from congress_models import (
    BillDetailResponse,
    BillSubjectsResponse,
    BillSummariesResponse,
    BillSummaryItem,
)
from psycopg2.extras import execute_values

CONGRESS_BILL_API = "https://api.congress.gov/v3/bill/"

BILLS_UPSERT_SQL = """
    INSERT INTO bills (
        congress, bill_type, bill_number, title, policy_area, crs_summary,
        source_hash, source_updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (congress, bill_type, bill_number) DO UPDATE SET
        title = EXCLUDED.title,
        policy_area = EXCLUDED.policy_area,
        crs_summary = EXCLUDED.crs_summary,
        source_hash = EXCLUDED.source_hash,
        source_updated_at = EXCLUDED.source_updated_at,
        synced_at = NOW(),
        updated_at = CASE
            WHEN bills.source_hash IS DISTINCT FROM EXCLUDED.source_hash
            THEN NOW()
            ELSE bills.updated_at
        END
    -- Deliberately no WHERE guard here (unlike MEMBERS_UPSERT_SQL) --
    -- this runs once per bill via get_or_sync_bill/bills_etl's refresh
    -- and RETURNING must always yield bill_id, even on a race between
    -- two overlapping syncs of the same not-yet-seen bill, or a refresh
    -- that finds nothing changed.
    RETURNING bill_id
"""

BILL_SUBJECTS_INSERT_SQL = """
    -- Plain insert, not an upsert -- the caller DELETEs a bill's
    -- existing subject rows first (full replace, per the schema's
    -- stated intent). ON CONFLICT DO NOTHING is a defensive backstop
    -- against a duplicate subject name within one fetch (the live API's
    -- own pagination.count was observed to be unreliable here).
    INSERT INTO bill_subjects (bill_id, subject_name) VALUES %s
    ON CONFLICT (bill_id, subject_name) DO NOTHING
"""


def _latest_crs_summary(summaries: list[BillSummaryItem]) -> str | None:
    # Congress.gov issues a new CRS summary at each legislative stage
    # (introduced, reported, enrolled, ...) -- the one with the latest
    # actionDate is treated as "the" current summary. Restricted to
    # entries that actually have text first: a data-quality issue in the
    # most-recently-dated entry (null/empty text -- this sub-resource has
    # already been observed elsewhere to have inconsistencies, see
    # BILL_SUBJECTS_INSERT_SQL's own comment) shouldn't discard an
    # earlier stage's perfectly usable summary.
    usable = [s for s in summaries if s.text]
    if not usable:
        return None
    return max(usable, key=lambda s: s.action_date or date.min).text


def sync_bill(
    session: requests.Session,
    conn: Any,
    congress: int,
    bill_type: str,
    bill_number: int,
) -> int:
    # Detail, subjects, and summaries are three independent endpoints --
    # fetched concurrently since none depends on another's result.
    bill_url = f"{CONGRESS_BILL_API}{congress}/{bill_type.lower()}/{bill_number}"
    with ThreadPoolExecutor(max_workers=3) as executor:
        detail_future = executor.submit(
            congress_api.api_get_model, session, bill_url, BillDetailResponse,
        )
        subjects_future = executor.submit(
            congress_api.api_get_model, session, f"{bill_url}/subjects", BillSubjectsResponse,
        )
        summaries_future = executor.submit(
            congress_api.api_get_model, session, f"{bill_url}/summaries", BillSummariesResponse,
        )
        bill = detail_future.result().bill
        subjects = subjects_future.result().subjects
        summaries = summaries_future.result().summaries

    policy_area = bill.policy_area_name
    crs_summary = _latest_crs_summary(summaries)

    with conn.cursor() as cursor:
        cursor.execute(
            BILLS_UPSERT_SQL,
            (
                congress, bill_type, bill_number, bill.title, policy_area, crs_summary,
                congress_api.source_hash(
                    congress, bill_type, bill_number, bill.title, policy_area, crs_summary,
                ),
                bill.update_date,
            ),
        )
        bill_id = cursor.fetchone()[0]

        cursor.execute("DELETE FROM bill_subjects WHERE bill_id = %s", (bill_id,))
        # Deduped defensively -- the /subjects sub-resource's own
        # pagination.count was observed live to not match its actual
        # legislativeSubjects array length.
        subject_names = list(dict.fromkeys(s.name for s in subjects.legislative_subjects))
        if subject_names:
            execute_values(
                cursor, BILL_SUBJECTS_INSERT_SQL,
                [(bill_id, name) for name in subject_names],
            )

    # Committed here, per bill, rather than once at the end of the
    # caller's loop over many bills -- so one bill's failure doesn't roll
    # back bills already synced earlier in the same run, and so a second
    # call for the same bill later in the same run sees this row via its
    # own SELECT instead of racing an INSERT (see house_votes_etl's
    # resolve_bills, which processes votes sequentially specifically
    # because of this).
    conn.commit()
    return bill_id
