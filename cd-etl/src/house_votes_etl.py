"""Syncs House roll call votes into roll_calls/roll_call_member_votes.

Bills are populated on demand, not proactively synced wholesale --
get_or_sync_bill() is called inline for each vote's linked legislation
(or its resolved amendment, for the ~12% of votes that reference one
instead of a bill directly), fetching and storing a bill only the
first time it's actually referenced by a vote. The 119th Congress has
18,140 bills total but only a few hundred are ever referenced by a
House vote, and roll_calls' whole purpose is deriving how a member
voted on a policy area -- a bill nobody voted on doesn't serve that,
so proactively syncing all 18,140 was rejected as wasted API calls
(see rchacon/cd-platform#8).

Purely procedural votes with no bill or amendment reference at all
(e.g. "Elected Speaker") are excluded entirely, same treatment as
nominations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from itertools import batched
from typing import Any

import congress_api
import requests
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from congress_models import (
    AmendmentDetail,
    BillDetail,
    BillSubjects,
    HouseVoteDetail,
    HouseVoteListItem,
    HouseVoteMemberVote,
)
from psycopg2.extras import execute_values
from pydantic import ValidationError

CONGRESS_BILL_API = "https://api.congress.gov/v3/bill/"
CONGRESS_AMENDMENT_API = "https://api.congress.gov/v3/amendment/"
CONGRESS_HOUSE_VOTE_API = "https://api.congress.gov/v3/house-vote/"

PAGE_LIMIT = 250
MEMBER_VOTES_FETCH_WORKERS = 10
POSTGRES_CONN_ID = "congressional_postgres"

# Bounds a load() failure's blast radius to one chunk of roll calls
# rather than either the whole run (one big transaction) or a full
# commit per roll call (too many WAL flushes at real vote volumes).
LOAD_CHUNK_SIZE = 50

_API_SESSION = congress_api.build_session(pool_maxsize=MEMBER_VOTES_FETCH_WORKERS)

# House roll calls report different literal values depending on voteType
# ("Recorded Vote" uses Aye/No/Not Voting; "Yea-and-Nay" uses Yea/Nay/Not
# Voting) -- normalized case-insensitively onto vote_cast_type, matching
# the rationale already documented on that enum in the schema migration.
VOTE_CAST_MAP = {
    "yea": "YEA",
    "aye": "YEA",
    "nay": "NAY",
    "no": "NAY",
    "present": "PRESENT",
    "not voting": "NOT_VOTING",
}

BILLS_UPSERT_SQL = """
    INSERT INTO bills (
        congress, bill_type, bill_number, policy_area, source_hash, source_updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (congress, bill_type, bill_number) DO UPDATE SET
        policy_area = EXCLUDED.policy_area,
        source_hash = EXCLUDED.source_hash,
        source_updated_at = EXCLUDED.source_updated_at,
        synced_at = NOW(),
        updated_at = CASE
            WHEN bills.source_hash IS DISTINCT FROM EXCLUDED.source_hash
            THEN NOW()
            ELSE bills.updated_at
        END
    -- Deliberately no WHERE guard here (unlike MEMBERS_UPSERT_SQL) --
    -- this runs once per bill via get_or_sync_bill and RETURNING must
    -- always yield bill_id, even on a race between two overlapping
    -- syncs of the same not-yet-seen bill.
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

logger = logging.getLogger(__name__)


def get_or_sync_bill(
    session: requests.Session,
    conn: Any,
    congress: int,
    bill_type: str,
    bill_number: int,
) -> int:
    # Sync-once, not a refresh path: once a bill is stored, this helper
    # never re-fetches it, even though bills.source_hash/source_updated_at
    # exist on the table (unused by this helper -- they're there for a
    # future bills-refresh path, not exercised here). Only bills actually
    # referenced by a vote are ever synced at all -- see house_votes_etl's
    # module docstring for why a full proactive bill sync was rejected.
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT bill_id FROM bills WHERE congress = %s AND bill_type = %s AND bill_number = %s",
            (congress, bill_type, bill_number),
        )
        row = cursor.fetchone()
    if row is not None:
        return row[0]

    raw_detail = congress_api.api_get(
        session, f"{CONGRESS_BILL_API}{congress}/{bill_type.lower()}/{bill_number}",
    )["bill"]
    bill = BillDetail.model_validate(raw_detail)

    raw_subjects = congress_api.api_get(
        session, f"{CONGRESS_BILL_API}{congress}/{bill_type.lower()}/{bill_number}/subjects",
    )["subjects"]
    subjects = BillSubjects.model_validate(raw_subjects)

    policy_area = bill.policy_area_name

    with conn.cursor() as cursor:
        cursor.execute(
            BILLS_UPSERT_SQL,
            (
                congress, bill_type, bill_number, policy_area,
                congress_api.source_hash(congress, bill_type, bill_number, policy_area),
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
    # caller's loop over many votes -- so one bill's failure doesn't roll
    # back bills already synced earlier in the same run, and so a second
    # call for the same bill later in the same run sees this row via its
    # own SELECT instead of racing an INSERT (see resolve_bills, which
    # processes votes sequentially specifically because of this).
    conn.commit()
    return bill_id


def resolve_amendment_bill(
    session: requests.Session, congress: int, amendment_type: str, amendment_number: str,
) -> tuple[int, str, int] | None:
    raw = congress_api.api_get(
        session, f"{CONGRESS_AMENDMENT_API}{congress}/{amendment_type.lower()}/{amendment_number}",
    )["amendment"]
    amendment = AmendmentDetail.model_validate(raw)

    if amendment.amended_bill is None:
        return None

    return (
        amendment.amended_bill.congress,
        amendment.amended_bill.type,
        int(amendment.amended_bill.number),
    )


@dag(
    dag_id="house_votes_etl",
    description="Sync House roll call votes into roll_calls/roll_call_member_votes",
    schedule="@daily",
    start_date=datetime(2025, 1, 3),
    catchup=False,
    default_args={"retries": 2},
    tags=["congress"],
)
def house_votes_etl():

    @task
    def get_current_congress() -> int:
        # Same query members_etl.py's task of the same name uses --
        # Postgres's own current_congress() function is the single place
        # every ETL agrees on "which Congress is current."
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        row = hook.get_first("SELECT current_congress()")
        if row is None or row[0] is None:
            raise ValueError("No current congress found in congresses table")
        return row[0]

    @task
    def extract_house_vote_summaries(congress: int) -> list[dict[str, Any]]:
        # Both sessions are fetched unconditionally every run -- nothing
        # in this schema stores "current session number," and paging an
        # empty/not-yet-started session 2 costs one cheap short-circuited
        # request.
        summaries = []
        for session_number in (1, 2):
            summaries.extend(
                congress_api.paginate(
                    _API_SESSION,
                    f"{CONGRESS_HOUSE_VOTE_API}{congress}/{session_number}",
                    {},
                    items_key="houseRollCallVotes",
                    page_limit=PAGE_LIMIT,
                )
            )

        logger.info(
            "Found %d House roll call votes across both sessions of the %dth Congress",
            len(summaries), congress,
        )
        return summaries

    @task
    def filter_votes_needing_sync(
        summaries: list[dict[str, Any]], congress: int,
    ) -> dict[str, Any]:
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        known_votes = {
            (row[0], row[1])
            for row in hook.get_records(
                "SELECT session, vote_number FROM roll_calls WHERE chamber = 'HOUSE' AND congress = %s",
                parameters=(congress,),
            )
        }
        known_bioguide_ids = [
            row[0] for row in hook.get_records("SELECT bioguide_id FROM members")
        ]

        votes = []
        already_known_count = 0
        dropped_procedural_count = 0
        for raw_summary in summaries:
            item = HouseVoteListItem.model_validate(raw_summary)

            if (item.session_number, item.roll_call_number) in known_votes:
                already_known_count += 1
                continue

            # A vote with neither a bill nor an amendment reference is
            # purely procedural (e.g. "Elected Speaker") and can never
            # get a bill_id -- excluded entirely before ingestion, same
            # precedent as nominations (see rchacon/cd-platform#8).
            if item.legislation_type is None and item.amendment_type is None:
                dropped_procedural_count += 1
                continue

            votes.append(raw_summary)

        logger.info(
            "%d of %d votes need syncing (%d already known, %d purely procedural dropped)",
            len(votes), len(summaries), already_known_count, dropped_procedural_count,
        )
        return {"votes": votes, "known_bioguide_ids": known_bioguide_ids}

    @task
    def resolve_bills(votes: list[dict[str, Any]], congress: int) -> list[dict[str, Any]]:
        # Deviation from members_etl.py's pure extract/transform split:
        # resolving bill_id means real DB reads/writes interleaved with
        # API calls per vote, since it's a foreign-key precondition, not
        # source data reachable by pure transformation. Processed
        # sequentially (not via congress_api.fetch_concurrently) because
        # two votes in this same batch can reference the same
        # not-yet-synced bill -- sequential processing lets the second
        # get_or_sync_bill call see the first's already-committed row via
        # its own SELECT, instead of racing an INSERT.
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()

        resolved = []
        try:
            for raw_summary in votes:
                item = HouseVoteListItem.model_validate(raw_summary)

                try:
                    if item.legislation_type is not None:
                        bill_key = (congress, item.legislation_type, int(item.legislation_number))
                    else:
                        bill_key = resolve_amendment_bill(
                            _API_SESSION, congress, item.amendment_type, item.amendment_number,
                        )
                        if bill_key is None:
                            logger.info(
                                "Skipping vote %d/%d: amendment has no resolvable bill",
                                item.session_number, item.roll_call_number,
                            )
                            continue

                    bill_id = get_or_sync_bill(_API_SESSION, conn, *bill_key)

                    raw_detail = congress_api.api_get(
                        _API_SESSION,
                        f"{CONGRESS_HOUSE_VOTE_API}{congress}/{item.session_number}/{item.roll_call_number}",
                    )["houseRollCallVote"]
                    detail = HouseVoteDetail.model_validate(raw_detail)
                except Exception as exc:
                    # Broad on purpose -- HTTP errors, pydantic
                    # ValidationError, a malformed legislation_number
                    # that fails int(), or a DB error can all happen
                    # here, and one vote's failure shouldn't abort the
                    # rest of the batch. conn.rollback() is required
                    # (not just logging) because a DB-side failure
                    # leaves the shared connection in an aborted-
                    # transaction state that would otherwise break every
                    # subsequent iteration's queries too.
                    conn.rollback()
                    logger.error(
                        "Skipping vote %d/%d: %s",
                        item.session_number, item.roll_call_number, exc,
                    )
                    continue

                resolved.append({
                    "session": item.session_number,
                    "roll_call_number": item.roll_call_number,
                    "bill_id": bill_id,
                    "vote_question": detail.vote_question,
                    "result": item.result,
                    "vote_date": item.start_date.date().isoformat(),
                })
        finally:
            conn.close()

        logger.info("Resolved %d of %d votes to a bill", len(resolved), len(votes))
        return resolved

    @task
    def fetch_member_votes(
        resolved_votes: list[dict[str, Any]], congress: int,
    ) -> list[dict[str, Any]]:
        def fetch_one(key: tuple[int, int]) -> dict[str, Any]:
            session_number, roll_call_number = key
            raw = congress_api.api_get(
                _API_SESSION,
                f"{CONGRESS_HOUSE_VOTE_API}{congress}/{session_number}/{roll_call_number}/members",
            )["houseRollCallVoteMemberVotes"]
            return {
                "session": session_number,
                "roll_call_number": roll_call_number,
                "votes": raw["results"],
            }

        keys = [(v["session"], v["roll_call_number"]) for v in resolved_votes]
        results = congress_api.fetch_concurrently(keys, fetch_one, MEMBER_VOTES_FETCH_WORKERS)

        logger.info(
            "Fetched member votes for %d of %d votes", len(results), len(resolved_votes),
        )
        return results

    @task
    def transform(
        resolved_votes: list[dict[str, Any]],
        member_votes: list[dict[str, Any]],
        known_bioguide_ids: list[str],
        congress: int,
    ) -> dict[str, list[Any]]:
        """Builds the two SQL-ready row lists load() upserts.

        Returns a dict with:
          "roll_calls": list of tuples, one per roll_call_rows in
            ROLL_CALLS_UPSERT_SQL's column order (chamber, congress,
            session, vote_number, bill_id, vote_question, result,
            vote_date, source_hash).
          "member_votes": list of {"key": [session, vote_number],
            "casts": [(bioguide_id, vote_cast), ...]} dicts -- "key"
            identifies which roll call these casts belong to (a plain
            list, not a tuple, since this return value crosses an
            Airflow XCom boundary and has to stay JSON-serializable).
            load() resolves "key" to the actual roll_call_id itself,
            since that's only known after its own roll_calls upsert.
        """
        member_votes_by_key = {
            (mv["session"], mv["roll_call_number"]): mv["votes"] for mv in member_votes
        }
        known_bioguide_id_set = set(known_bioguide_ids)

        roll_call_rows = []
        member_vote_rows = []
        dropped_unknown_bioguide_count = 0
        skipped_missing_member_votes_count = 0

        for vote in resolved_votes:
            key = (vote["session"], vote["roll_call_number"])
            raw_casts = member_votes_by_key.get(key)
            if raw_casts is None:
                # This vote's member-vote fetch failed or was skipped --
                # dropped here rather than stored with zero casts.
                # Incremental sync (filter_votes_needing_sync) is a pure
                # existence check with no separate retry path, so a
                # roll_calls row ever committed without its member votes
                # in the same transaction would be permanently stuck --
                # a future run would never revisit a key already present.
                skipped_missing_member_votes_count += 1
                continue

            casts = []
            for raw_cast in raw_casts:
                try:
                    member_vote = HouseVoteMemberVote.model_validate(raw_cast)
                    vote_cast = VOTE_CAST_MAP[member_vote.vote_cast.strip().lower()]
                except (KeyError, TypeError, ValidationError) as exc:
                    logger.error(
                        "Skipping one member vote on session %d roll call %d: "
                        "malformed API data (%s)",
                        key[0], key[1], exc,
                    )
                    continue

                if member_vote.bioguide_id not in known_bioguide_id_set:
                    # Defensive: roll_call_member_votes.bioguide_id has a
                    # hard FK to members. One unknown id in a batch would
                    # otherwise fail the whole execute_values insert, not
                    # just that one row.
                    dropped_unknown_bioguide_count += 1
                    continue

                casts.append((member_vote.bioguide_id, vote_cast))

            source_hash = congress_api.source_hash(
                "HOUSE", congress, vote["session"], vote["roll_call_number"], vote["bill_id"],
                vote["vote_question"], vote["result"], vote["vote_date"],
            )
            roll_call_rows.append((
                "HOUSE", congress, vote["session"], vote["roll_call_number"], vote["bill_id"],
                vote["vote_question"], vote["result"], vote["vote_date"], source_hash,
            ))
            member_vote_rows.append({"key": list(key), "casts": casts})

        logger.info(
            "Transformed %d roll calls (%d skipped for missing member votes, "
            "%d member votes dropped for unknown bioguide_id)",
            len(roll_call_rows), skipped_missing_member_votes_count,
            dropped_unknown_bioguide_count,
        )
        return {"roll_calls": roll_call_rows, "member_votes": member_vote_rows}

    @task
    def load(rows: dict[str, list[Any]]) -> None:
        # Committed in chunks of LOAD_CHUNK_SIZE roll calls rather than
        # one transaction for the whole run (which would roll back
        # everything on a single bad row) or one transaction per roll
        # call (correspondingly more commits/WAL flushes than needed at
        # real vote volumes) -- a failed chunk only costs that chunk,
        # logged and skipped, while the rest of the run's roll calls
        # still land. A roll_calls row is never committed without its
        # own member votes: both upserts for a chunk share the same
        # transaction, so that pairing holds per chunk even though the
        # run as a whole isn't one single transaction anymore.
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()

        member_votes_by_key = {
            tuple(mv["key"]): mv["casts"] for mv in rows["member_votes"]
        }
        loaded_roll_calls = 0
        loaded_votes = 0
        failed_chunks = 0

        try:
            for chunk in batched(rows["roll_calls"], LOAD_CHUNK_SIZE):
                chunk = list(chunk)
                try:
                    with conn.cursor() as cursor:
                        execute_values(cursor, ROLL_CALLS_UPSERT_SQL, chunk)

                        # ROLL_CALLS_UPSERT_SQL's ON CONFLICT is
                        # WHERE-gated on source_hash, so RETURNING would
                        # silently omit any row Postgres decided not to
                        # update -- a follow-up SELECT on the natural key
                        # is used instead to reliably get every
                        # roll_call_id this chunk needs (unlike
                        # BILLS_UPSERT_SQL, which is single-row and can
                        # safely use RETURNING because its ON CONFLICT is
                        # deliberately unconditional).
                        congress = chunk[0][1]
                        sessions = [row[2] for row in chunk]
                        vote_numbers = [row[3] for row in chunk]
                        cursor.execute(
                            """
                            SELECT roll_call_id, session, vote_number FROM roll_calls
                            WHERE chamber = 'HOUSE' AND congress = %s
                              AND session = ANY(%s) AND vote_number = ANY(%s)
                            """,
                            (congress, sessions, vote_numbers),
                        )
                        roll_call_id_by_key = {
                            (session, vote_number): roll_call_id
                            for roll_call_id, session, vote_number in cursor.fetchall()
                        }

                        member_vote_rows = []
                        for row in chunk:
                            key = (row[2], row[3])
                            roll_call_id = roll_call_id_by_key.get(key)
                            if roll_call_id is None:
                                continue
                            for bioguide_id, vote_cast in member_votes_by_key.get(key, []):
                                member_vote_rows.append((roll_call_id, bioguide_id, vote_cast))

                        if member_vote_rows:
                            execute_values(
                                cursor, ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, member_vote_rows,
                            )

                    conn.commit()
                    loaded_roll_calls += len(chunk)
                    loaded_votes += len(member_vote_rows)
                except Exception as exc:
                    # A chunk's own failure shouldn't abort chunks
                    # already committed, or chunks still to come --
                    # rollback resets the shared connection to a usable
                    # state for the next iteration (same reason
                    # resolve_bills rolls back on a per-vote failure).
                    conn.rollback()
                    failed_chunks += 1
                    logger.error(
                        "Failed to load a chunk of %d roll calls (%s): %s",
                        len(chunk), [(row[2], row[3]) for row in chunk], exc,
                    )
        finally:
            conn.close()

        logger.info(
            "Loaded %d roll calls and %d member votes (%d chunk(s) failed)",
            loaded_roll_calls, loaded_votes, failed_chunks,
        )

    congress = get_current_congress()
    summaries = extract_house_vote_summaries(congress)
    filtered = filter_votes_needing_sync(summaries, congress)
    resolved = resolve_bills(filtered["votes"], congress)
    member_votes = fetch_member_votes(resolved, congress)
    load(transform(resolved, member_votes, filtered["known_bioguide_ids"], congress))


house_votes_etl()
