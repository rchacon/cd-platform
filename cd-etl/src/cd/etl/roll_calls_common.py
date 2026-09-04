"""Shared roll-call vote machinery, used by both chamber vote DAGs.

`house_votes_etl` and `senate_votes_etl` differ only in their *source*
(Congress.gov's JSON house-vote API vs. the Senate's public XML feed);
once a vote has been reduced to `(bill_type, bill_number, session,
vote_number, question, result, date)` plus a list of per-member casts,
resolving its bill on demand and upserting into `roll_calls` /
`roll_call_member_votes` with the same conflict semantics is identical.
That shared part lives here, mirroring `bills_common.py`; chamber-specific
extraction stays in each DAG module.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from cd.etl import bills_common

logger = logging.getLogger(__name__)

# House roll calls report Aye/No ("Recorded Vote") or Yea/Nay
# ("Yea-and-Nay"); Senate roll calls report Yea/Nay/Present/Not Voting.
# Normalized case-insensitively onto `vote_cast_type` -- see that enum's
# own comment in migration 0002 for the rationale.
VOTE_CAST_MAP = {
    "yea": "YEA",
    "aye": "YEA",
    "nay": "NAY",
    "no": "NAY",
    "present": "PRESENT",
    "not voting": "NOT_VOTING",
}

ROLL_CALLS_UPSERT_SQL = """
    INSERT INTO roll_calls (
        chamber, congress, session, vote_number, bill_id,
        vote_question, result, vote_date, source_hash
    )
    VALUES %s
    ON CONFLICT (chamber, congress, session, vote_number) DO UPDATE SET
        bill_id = EXCLUDED.bill_id,
        vote_question = EXCLUDED.vote_question,
        result = EXCLUDED.result,
        vote_date = EXCLUDED.vote_date,
        source_hash = EXCLUDED.source_hash,
        synced_at = NOW(),
        updated_at = CASE
            WHEN roll_calls.source_hash IS DISTINCT FROM EXCLUDED.source_hash
            THEN NOW()
            ELSE roll_calls.updated_at
        END
    WHERE roll_calls.source_hash IS DISTINCT FROM EXCLUDED.source_hash
"""

ROLL_CALL_MEMBER_VOTES_UPSERT_SQL = """
    INSERT INTO roll_call_member_votes (roll_call_id, bioguide_id, vote_cast)
    VALUES %s
    ON CONFLICT (roll_call_id, bioguide_id) DO UPDATE SET
        vote_cast = EXCLUDED.vote_cast,
        updated_at = NOW()
    WHERE roll_call_member_votes.vote_cast IS DISTINCT FROM EXCLUDED.vote_cast
"""


def get_or_sync_bill(
    session: requests.Session,
    conn: Any,
    congress: int,
    bill_type: str,
    bill_number: int,
    bedrock_client: Any,
) -> int:
    # Sync-once, not a refresh path: once a bill is stored, this helper
    # never re-fetches it. Only bills actually referenced by a vote are
    # ever synced at all -- see house_votes_etl's module docstring for
    # why a full proactive bill sync was rejected, and for where the
    # refresh path lives instead (bills_etl.py, via the same
    # bills_common.sync_bill this calls on a cache miss).
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT bill_id FROM bills WHERE congress = %s AND bill_type = %s AND bill_number = %s",
            (congress, bill_type, bill_number),
        )
        row = cursor.fetchone()
    if row is not None:
        return row[0]

    return bills_common.sync_bill(session, conn, congress, bill_type, bill_number, bedrock_client)
