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

get_or_sync_bill() is deliberately sync-once, not a refresh path: on a
cache hit it returns the existing bill_id without re-fetching. Keeping
an already-synced bill's policy_area/subjects/CRS summary current is
bills_etl's job (see bills_etl.py, resolving rchacon/cd-platform#52) --
a separate, independently-scheduled DAG rather than a trigger off this
one, since staleness of an already-known bill is a downstream reader's
problem, not a vote-sync correctness problem. Both DAGs call the same
fetch+upsert logic in bills_common.sync_bill().

Purely procedural votes with no bill or amendment reference at all
(e.g. "Elected Speaker") are excluded entirely, same treatment as
nominations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from itertools import batched
from typing import Any

import requests
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from cd.etl import bills_common, congress_api
from cd.etl.congress_models import (
    AmendmentResponse,
    HouseVoteDetailResponse,
    HouseVoteListItem,
    HouseVoteMemberVote,
    HouseVoteMemberVotesResponse,
)
from psycopg2.extras import execute_values
from pydantic import ValidationError

CONGRESS_AMENDMENT_API = "https://api.congress.gov/v3/amendment/"
CONGRESS_HOUSE_VOTE_API = "https://api.congress.gov/v3/house-vote/"

PAGE_LIMIT = 250
MEMBER_VOTES_FETCH_WORKERS = 10
POSTGRES_CONN_ID = "congressional_postgres"

# Bounds sync_member_votes()'s peak memory to one batch's worth of
# member-vote casts (~VOTE_BATCH_SIZE votes x ~435 House members) rather
# than the whole run's -- see rchacon/cd-platform#59, where fetching and
# holding every synced vote's full member breakdown at once repeatedly
# OOM-killed the Airflow worker on a backfill/catch-up day. Also bounds
# the transaction/commit chunk size, the same rationale this constant
# (formerly LOAD_CHUNK_SIZE) had before it started governing the API
# fetch too. Not shrunk below 50: MEMBER_VOTES_FETCH_WORKERS already caps
# fetch concurrency at 10, so a smaller batch buys no further memory
# benefit while adding more DB round trips and serializing fetch+write
# across more batches.
VOTE_BATCH_SIZE = 50

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

    return bills_common.sync_bill(session, conn, congress, bill_type, bill_number)


def resolve_amendment_bill(
    session: requests.Session, congress: int, amendment_type: str, amendment_number: str,
) -> tuple[int, str, int] | None:
    amendment = congress_api.api_get_model(
        session,
        f"{CONGRESS_AMENDMENT_API}{congress}/{amendment_type.lower()}/{amendment_number}",
        AmendmentResponse,
    ).amendment

    if amendment.amended_bill is None:
        return None

    return (
        amendment.amended_bill.congress,
        amendment.amended_bill.type,
        int(amendment.amended_bill.number),
    )


def _build_batch_rows(
    batch: list[dict[str, Any]],
    vote_question_by_key: dict[tuple[int, int], str],
    fetched_by_key: dict[tuple[int, int], list[dict[str, Any]]],
    known_bioguide_id_set: set[str],
    congress: int,
) -> tuple[list[tuple[Any, ...]], dict[tuple[int, int], list[tuple[str, str]]], dict[str, int]]:
    """Builds one batch's SQL-ready roll_calls rows and member-vote casts.

    Pure -- no DB/API calls -- so sync_member_votes() can call this once
    per batch without holding more than one batch's data in memory at a
    time (rchacon/cd-platform#59). Unlike the old transform() this
    replaces, casts_by_key uses real (session, vote_number) tuple keys
    directly: this never crosses an Airflow XCom boundary (called
    in-process from sync_member_votes()), so the list(key)/{"key": ...}
    JSON-safety wrapping transform() needed doesn't apply here.

    The third return value, skip_counts, is a per-batch skip-reason
    breakdown (same keys every call) -- sync_member_votes() accumulates
    these across every batch so the run's final summary line still
    reports one aggregate total, the way the old fetch_member_votes/
    transform's single per-run log line did, rather than only ever
    appearing scattered across dozens of per-batch log lines on a large
    backfill.
    """
    roll_call_rows = []
    casts_by_key: dict[tuple[int, int], list[tuple[str, str]]] = {}
    dropped_unknown_bioguide_count = 0
    skipped_missing_detail_count = 0
    skipped_missing_member_votes_count = 0
    skipped_zero_valid_casts_count = 0

    for vote in batch:
        key = (vote["session"], vote["roll_call_number"])

        vote_question = vote_question_by_key.get(key)
        if vote_question is None:
            # This vote's detail fetch (vote_question) failed or was
            # skipped -- can't build a roll_calls row without it
            # (a NOT NULL column), same reasoning as the
            # missing-member-votes case below.
            skipped_missing_detail_count += 1
            continue

        raw_casts = fetched_by_key.get(key)
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

        if not casts:
            # Every cast for this vote was dropped (malformed data,
            # or none of its voters are known to us yet) -- same
            # reasoning as the raw_casts-is-None case above: a
            # roll_calls row with zero real casts would look
            # "already synced" forever, with no way to backfill it
            # once the missing member(s) are known.
            skipped_zero_valid_casts_count += 1
            continue

        source_hash = congress_api.source_hash(
            "HOUSE", congress, vote["session"], vote["roll_call_number"], vote["bill_id"],
            vote_question, vote["result"], vote["vote_date"],
        )
        roll_call_rows.append((
            "HOUSE", congress, vote["session"], vote["roll_call_number"], vote["bill_id"],
            vote_question, vote["result"], vote["vote_date"], source_hash,
        ))
        casts_by_key[key] = casts

    logger.info(
        "Built %d roll calls for this batch (%d skipped for missing detail, "
        "%d skipped for missing member votes, %d skipped for zero valid casts, "
        "%d member votes dropped for unknown bioguide_id)",
        len(roll_call_rows), skipped_missing_detail_count,
        skipped_missing_member_votes_count, skipped_zero_valid_casts_count,
        dropped_unknown_bioguide_count,
    )
    skip_counts = {
        "missing_detail": skipped_missing_detail_count,
        "missing_member_votes": skipped_missing_member_votes_count,
        "zero_valid_casts": skipped_zero_valid_casts_count,
        "dropped_unknown_bioguide": dropped_unknown_bioguide_count,
    }
    return roll_call_rows, casts_by_key, skip_counts


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
        return congress_api.get_current_congress(POSTGRES_CONN_ID)

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
        """Narrows summaries down to the votes later tasks actually need to sync.

        A raw summary is dropped (not returned in "votes") if it's
        malformed, already has a roll_calls row for its (session,
        vote_number), or is purely procedural (no bill or amendment
        reference at all, e.g. "Elected Speaker").

        Returns a dict with:
          "votes": list of raw summary dicts (unvalidated -- re-validated
            by resolve_bills) needing a sync this run.
          "known_bioguide_ids": list of every bioguide_id currently in
            members, passed through so transform() can defensively drop
            a member vote referencing someone not in members yet.
        """
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
        dropped_malformed_count = 0
        for raw_summary in summaries:
            try:
                item = HouseVoteListItem.model_validate(raw_summary)
            except ValidationError as exc:
                # One malformed vote summary shouldn't crash this task
                # (and with it the whole DAG run) -- logged and skipped,
                # same fault-isolation philosophy as every other
                # per-item loop in this module.
                dropped_malformed_count += 1
                logger.error("Skipping malformed vote summary: %s", exc)
                continue

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
            "%d of %d votes need syncing (%d already known, %d purely procedural dropped, "
            "%d malformed dropped)",
            len(votes), len(summaries), already_known_count, dropped_procedural_count,
            dropped_malformed_count,
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
                        if bill_key[0] != congress:
                            # An amendment's own reported bill congress
                            # should always equal the Congress currently
                            # being synced -- amendments only exist
                            # during the live legislative process of
                            # their own Congress. Treated as a
                            # resolution failure rather than silently
                            # trusted: roll_calls.congress is this
                            # task's congress, not the bill's, so
                            # inserting under a mismatched congress would
                            # point a roll call at the wrong bill_id
                            # instead of failing loudly.
                            raise ValueError(
                                f"Amendment's resolved bill congress ({bill_key[0]}) does not "
                                f"match the currently-synced congress ({congress})"
                            )

                    bill_id = get_or_sync_bill(_API_SESSION, conn, *bill_key)
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
                    "result": item.result,
                    "vote_date": item.start_date.date().isoformat(),
                })
        finally:
            conn.close()

        logger.info("Resolved %d of %d votes to a bill", len(resolved), len(votes))
        return resolved

    @task
    def fetch_vote_details(
        resolved_votes: list[dict[str, Any]], congress: int,
    ) -> list[dict[str, Any]]:
        # Split out of resolve_bills into its own concurrent task -- this
        # detail fetch (only needed for vote_question) has no ordering
        # dependency on bill resolution, unlike get_or_sync_bill's own
        # calls, so it doesn't need resolve_bills's sequential-per-vote
        # treatment and can run fully concurrently via
        # congress_api.fetch_concurrently instead, same as
        # sync_member_votes's own per-batch member-vote fetch.
        def fetch_one(key: tuple[int, int]) -> dict[str, Any]:
            session_number, roll_call_number = key
            detail = congress_api.api_get_model(
                _API_SESSION,
                f"{CONGRESS_HOUSE_VOTE_API}{congress}/{session_number}/{roll_call_number}",
                HouseVoteDetailResponse,
            ).house_roll_call_vote
            return {
                "session": session_number,
                "roll_call_number": roll_call_number,
                "vote_question": detail.vote_question,
            }

        keys = [(v["session"], v["roll_call_number"]) for v in resolved_votes]
        results = congress_api.fetch_concurrently(keys, fetch_one, MEMBER_VOTES_FETCH_WORKERS)

        logger.info(
            "Fetched vote question detail for %d of %d votes", len(results), len(resolved_votes),
        )
        return results

    @task
    def sync_member_votes(
        resolved_votes: list[dict[str, Any]],
        vote_details: list[dict[str, Any]],
        known_bioguide_ids: list[str],
        congress: int,
    ) -> None:
        # Processes resolved_votes in batches of VOTE_BATCH_SIZE --
        # fetching, validating, and writing each batch before moving to
        # the next -- rather than fetching every vote's full ~435-member
        # breakdown into memory upfront the way the old
        # fetch_member_votes -> transform -> load chain did. That shape
        # repeatedly OOM-killed the Airflow worker on a backfill/catch-up
        # day (rchacon/cd-platform#59); this bounds peak memory to one
        # batch's worth of casts instead of the whole run's.
        #
        # Each batch is committed in one transaction (roll_calls +
        # roll_call_member_votes together) rather than either one
        # transaction for the whole run (rolls back everything on a
        # single bad row) or one commit per roll call (too many WAL
        # flushes at real vote volumes) -- a failed batch only costs that
        # batch, logged and skipped. A roll_calls row is never committed
        # without its own member votes: _build_batch_rows only produces a
        # row for a vote whose member-vote fetch already succeeded, and
        # both upserts for a batch share the same transaction, preserving
        # the same invariant the old load() had at chunk granularity --
        # filter_votes_needing_sync() has no separate retry path, so a
        # roll_calls row ever committed without its member votes would be
        # permanently stuck.
        #
        # A fresh connection per batch (via IsolatedTransaction), opened
        # only right before this batch's DB work rather than one
        # connection held open for the whole task -- the fetch above is
        # network-bound and can take tens of seconds per batch, and a
        # connection sitting open-but-idle across that phase is exactly
        # what an infra-level idle-connection timeout would catch,
        # potentially between batches rather than during one.
        def fetch_one(key: tuple[int, int]) -> dict[str, Any]:
            session_number, roll_call_number = key
            member_votes = congress_api.api_get_model(
                _API_SESSION,
                f"{CONGRESS_HOUSE_VOTE_API}{congress}/{session_number}/{roll_call_number}/members",
                HouseVoteMemberVotesResponse,
            ).house_roll_call_vote_member_votes
            return {
                "session": session_number,
                "roll_call_number": roll_call_number,
                "votes": member_votes.results,
            }

        vote_question_by_key = {
            (vd["session"], vd["roll_call_number"]): vd["vote_question"] for vd in vote_details
        }
        known_bioguide_id_set = set(known_bioguide_ids)
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)

        loaded_roll_calls = 0
        loaded_votes = 0
        failed_batches = 0
        # Accumulated across every batch so the run's final summary line
        # still reports one aggregate total, matching the old
        # fetch_member_votes/transform's single per-run log line -- see
        # _build_batch_rows()'s own docstring.
        total_skip_counts = {
            "missing_detail": 0, "missing_member_votes": 0,
            "zero_valid_casts": 0, "dropped_unknown_bioguide": 0,
        }

        for raw_batch in batched(resolved_votes, VOTE_BATCH_SIZE):
            batch = list(raw_batch)
            keys = [(v["session"], v["roll_call_number"]) for v in batch]
            fetched = congress_api.fetch_concurrently(keys, fetch_one, MEMBER_VOTES_FETCH_WORKERS)
            fetched_by_key = {(f["session"], f["roll_call_number"]): f["votes"] for f in fetched}

            roll_call_rows, casts_by_key, skip_counts = _build_batch_rows(
                batch, vote_question_by_key, fetched_by_key, known_bioguide_id_set, congress,
            )
            for reason, count in skip_counts.items():
                total_skip_counts[reason] += count
            if not roll_call_rows:
                # Every vote in this batch failed validation (e.g. every
                # member-vote fetch failed) -- nothing to write.
                # execute_values() would raise on an empty argslist, so
                # this must be checked before ever touching the DB.
                continue

            txn = congress_api.IsolatedTransaction(
                hook,
                f"a batch of {len(batch)} votes "
                f"({[(v['session'], v['roll_call_number']) for v in batch]})",
            )
            with txn as conn:
                with conn.cursor() as cursor:
                    execute_values(cursor, ROLL_CALLS_UPSERT_SQL, roll_call_rows)

                    # ROLL_CALLS_UPSERT_SQL's ON CONFLICT is WHERE-gated
                    # on source_hash, so RETURNING would silently omit
                    # any row Postgres decided not to update -- a
                    # follow-up SELECT on the natural key is used instead
                    # to reliably get every roll_call_id this batch needs
                    # (unlike bills_common.BILLS_UPSERT_SQL, which is
                    # single-row and can safely use RETURNING because its
                    # ON CONFLICT is deliberately unconditional).
                    sessions = [row[2] for row in roll_call_rows]
                    vote_numbers = [row[3] for row in roll_call_rows]
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
                    for key, casts in casts_by_key.items():
                        roll_call_id = roll_call_id_by_key.get(key)
                        if roll_call_id is None:
                            continue
                        for bioguide_id, vote_cast in casts:
                            member_vote_rows.append((roll_call_id, bioguide_id, vote_cast))

                    if member_vote_rows:
                        execute_values(
                            cursor, ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, member_vote_rows,
                        )

                loaded_roll_calls += len(roll_call_rows)
                loaded_votes += len(member_vote_rows)
            if txn.failed:
                failed_batches += 1

        logger.info(
            "Loaded %d roll calls and %d member votes (%d batch(es) failed, "
            "%d skipped for missing detail, %d skipped for missing member votes, "
            "%d skipped for zero valid casts, %d member votes dropped for unknown bioguide_id "
            "across the whole run)",
            loaded_roll_calls, loaded_votes, failed_batches,
            total_skip_counts["missing_detail"], total_skip_counts["missing_member_votes"],
            total_skip_counts["zero_valid_casts"], total_skip_counts["dropped_unknown_bioguide"],
        )

    congress = get_current_congress()
    summaries = extract_house_vote_summaries(congress)
    filtered = filter_votes_needing_sync(summaries, congress)
    resolved = resolve_bills(filtered["votes"], congress)
    vote_details = fetch_vote_details(resolved, congress)
    sync_member_votes(resolved, vote_details, filtered["known_bioguide_ids"], congress)


house_votes_etl()
