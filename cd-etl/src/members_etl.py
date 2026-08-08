from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import congress_api
import yaml
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from congress_models import (
    CongressCurrent,
    CongressCurrentResponse,
    MemberDetail,
    MemberSummary,
    PartyHistoryEntry,
)
from psycopg2.extras import Json, execute_values
from pydantic import ValidationError

CONGRESS_MEMBERS_API = "https://api.congress.gov/v3/member/"
CONGRESS_CURRENT_CONGRESS_API = "https://api.congress.gov/v3/congress/current"

# Not api.congress.gov -- a separate, public, unauthenticated source (see
# _crosswalk_row's own comment for why this crosswalk can't come from
# Congress.gov itself). Fetched directly with _API_SESSION.get(...), not
# congress_api.api_get, since that helper hardcodes the Congress.gov API
# key header and a format=json param, neither of which applies here (this
# is YAML, not JSON).
LEGISLATORS_CROSSWALK_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/"
    "main/legislators-current.yaml"
)

PAGE_LIMIT = 250
DETAIL_FETCH_WORKERS = 10
POSTGRES_CONN_ID = "congressional_postgres"

_API_SESSION = congress_api.build_session(pool_maxsize=DETAIL_FETCH_WORKERS)


MEMBERS_UPSERT_SQL = """
    INSERT INTO members (
        bioguide_id, given_name, middle_name, family_name,
        nickname, suffix, birth_year, death_year, photo_uri,
        phone, website_url, party_history, source_hash,
        source_updated_at
    )
    VALUES %s
    ON CONFLICT (bioguide_id) DO UPDATE SET
        given_name = EXCLUDED.given_name,
        middle_name = EXCLUDED.middle_name,
        family_name = EXCLUDED.family_name,
        nickname = EXCLUDED.nickname,
        suffix = EXCLUDED.suffix,
        birth_year = EXCLUDED.birth_year,
        death_year = EXCLUDED.death_year,
        photo_uri = EXCLUDED.photo_uri,
        phone = EXCLUDED.phone,
        website_url = EXCLUDED.website_url,
        party_history = EXCLUDED.party_history,
        source_hash = EXCLUDED.source_hash,
        source_updated_at = EXCLUDED.source_updated_at,
        synced_at = NOW(),
        -- updated_at reflects a real content change (source_hash),
        -- not just a fresh source_updated_at -- otherwise it would
        -- bump every time the source's timestamp ticks even when
        -- nothing we store actually changed.
        updated_at = CASE
            WHEN members.source_hash IS DISTINCT FROM EXCLUDED.source_hash
            THEN NOW()
            ELSE members.updated_at
        END
    WHERE members.source_hash IS DISTINCT FROM EXCLUDED.source_hash
       OR members.source_updated_at IS DISTINCT FROM EXCLUDED.source_updated_at
"""

MEMBER_TERMS_UPSERT_SQL = """
    INSERT INTO member_terms (
        bioguide_id, congress, chamber, member_type, state,
        district, start_year, end_year, source_hash
    )
    VALUES %s
    ON CONFLICT (bioguide_id, congress, chamber, state, district, start_year)
    DO UPDATE SET
        member_type = EXCLUDED.member_type,
        end_year = EXCLUDED.end_year,
        source_hash = EXCLUDED.source_hash,
        synced_at = NOW(),
        updated_at = NOW()
    WHERE member_terms.source_hash IS DISTINCT FROM EXCLUDED.source_hash
"""

# A plain guarded UPDATE, not an upsert -- this never creates a members row
# (that's MEMBERS_UPSERT_SQL's job, above); a crosswalk entry for a
# bioguide_id not yet in members simply matches zero rows. No source_hash
# column for this pair (only two nullable fields, cheap to compare
# directly) -- the IS DISTINCT FROM guard alone is enough to keep
# updated_at from bumping on every unchanged row every day.
LEGISLATORS_CROSSWALK_UPDATE_SQL = """
    UPDATE members SET
        lis_member_id = v.lis_member_id,
        senate_state_rank = v.senate_state_rank::senate_state_rank_type,
        updated_at = NOW()
    FROM (VALUES %s) AS v(bioguide_id, lis_member_id, senate_state_rank)
    WHERE members.bioguide_id = v.bioguide_id
      AND (members.lis_member_id IS DISTINCT FROM v.lis_member_id
           OR members.senate_state_rank IS DISTINCT FROM v.senate_state_rank::senate_state_rank_type)
"""

CHAMBER_MAP = {
    "House of Representatives": "HOUSE",
    "Senate": "SENATE",
}

# Anything not listed here (e.g. minor third parties) normalizes to OTHER.
# The original upstream string is always preserved in source_party_name.
PARTY_MAP = {
    "Democratic": "DEMOCRATIC",
    "Republican": "REPUBLICAN",
    "Independent": "INDEPENDENT",
    "Independent Democrat": "DEMOCRATIC",
    "Independent Republican": "REPUBLICAN",
    "Libertarian": "LIBERTARIAN",
    "Green": "GREEN",
}

# Matches senate_state_rank_type's own two values (migration 0003).
# Validated here, in Python, rather than trusting the ::senate_state_rank_type
# cast in LEGISLATORS_CROSSWALK_UPDATE_SQL to reject anything else -- that
# cast runs inside one batched execute_values() call covering every
# crosswalk row, so one unexpected value there would fail (and roll back)
# every other valid row in the same run, not just the bad one.
SENATE_STATE_RANKS = {"SENIOR", "JUNIOR"}

logger = logging.getLogger(__name__)


def _derive_congress_dates(congress: CongressCurrent) -> tuple[int, date, date]:
    # The API's own "endYear" is a generalized label (off by one from
    # the actual term-end date), so the real end date is derived as
    # exactly two years after the earliest session start date -- the
    # same convention init.sql used to seed the 119th Congress.
    start_date = min(
        (
            session.start_date
            for session in congress.sessions
            if session.start_date is not None
        ),
        default=date(congress.start_year, 1, 3),
    )
    end_date = date(congress.start_year + 2, 1, 3)
    return congress.number, start_date, end_date


def _members_needing_sync(
    summaries: list[dict[str, Any]],
    stored_updated_at: dict[str, datetime | None],
    bioguide_ids_with_current_term: set[str],
) -> list[str]:
    # Only re-fetch full detail for members that are new to us, whose
    # source record has changed since our last sync, or who don't yet
    # have a member_terms row for the current Congress -- the detail
    # endpoint is one call per member, so skipping unchanged members
    # avoids hundreds of needless requests on a typical day.
    #
    # summaries are validated here, at the point their fields actually
    # get read, rather than at extract_member_summaries -- consistent
    # with how transform() below validates member detail dicts at the
    # point of use rather than at fetch time (see its own comment for
    # why: crossing an Airflow XCom boundary requires plain JSON-safe
    # dicts, not pydantic model instances).
    stale_or_new = []
    for raw_summary in summaries:
        summary = MemberSummary.model_validate(raw_summary)
        bioguide_id = summary.bioguide_id
        last_synced = stored_updated_at.get(bioguide_id)
        source_updated = summary.update_date

        # A returning incumbent's bio-level updateDate may not change
        # just because a new Congress started -- relying on that alone
        # would silently skip creating their member_terms row for the
        # new Congress forever. Checking membership in
        # bioguide_ids_with_current_term directly, rather than assuming
        # anything about how/whether the source's updateDate reflects
        # term-only changes, is what actually guarantees a member isn't
        # missed on a Congress rollover.
        needs_term_sync = bioguide_id not in bioguide_ids_with_current_term

        # source_updated is None if the API ever omits/malforms
        # updateDate for a member -- never observed in practice, but if
        # it happens we can't tell whether they changed, so re-fetch
        # rather than risk silently skipping a real update forever.
        needs_bio_sync = (
            last_synced is None or source_updated is None or source_updated > last_synced
        )

        if needs_bio_sync or needs_term_sync:
            stale_or_new.append(bioguide_id)

    return stale_or_new


def _count_missing_start_year(party_history: list[PartyHistoryEntry]) -> int:
    return sum(1 for entry in party_history if entry.start_year is None)


def _party_history(party_history: list[PartyHistoryEntry]) -> list[dict[str, Any]]:
    # An entry with no start_year can't be placed chronologically, so it
    # can never correctly answer "which is the most recent party" --
    # that's unusable data, not just imprecise data, so it's dropped
    # rather than stored. Dropped-entry counts are surfaced via
    # transform()'s summary log line.
    #
    # Sorted by start_year rather than trusting upstream array order, so
    # source_hash is a function of the data and not of however the API
    # happens to order its response -- the API doesn't document any
    # ordering guarantee for this array.
    return sorted(
        (
            {
                "party": PARTY_MAP.get(entry.party_name, "OTHER"),
                "source_party_name": entry.party_name,
                "start_year": entry.start_year,
                "end_year": entry.end_year,
            }
            for entry in party_history
            if entry.start_year is not None
        ),
        key=lambda period: period["start_year"],
    )


def _member_row(member: MemberDetail) -> tuple[Any, ...]:
    party_history = _party_history(member.party_history)
    photo_uri = member.depiction.image_url
    phone = member.address_information.phone_number

    return (
        member.bioguide_id,
        member.first_name,
        member.middle_name,
        member.last_name,
        member.nick_name,
        member.suffix_name,
        member.birth_year,
        member.death_year,
        photo_uri,
        phone,
        member.official_website_url,
        party_history,
        congress_api.source_hash(
            member.bioguide_id, member.first_name, member.middle_name,
            member.last_name, member.nick_name, member.suffix_name,
            member.birth_year, member.death_year, photo_uri, phone,
            member.official_website_url, party_history,
        ),
        member.update_date,
    )


def _wrap_party_history_for_insert(
    member_rows: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    # party_history (index 11) is wrapped in Json(...) here, right
    # before the insert, rather than in _member_row/transform --
    # transform's return value crosses an XCom boundary (serialized to
    # JSON in the metadata DB), and psycopg2's Json wrapper isn't
    # something Airflow's XCom serializer knows how to encode.
    return [(*row[:11], Json(row[11]), *row[12:]) for row in member_rows]


def _term_rows(member: MemberDetail, congress: int) -> list[tuple[Any, ...]]:
    bioguide_id = member.bioguide_id

    rows = []
    for term in member.terms:
        # The API returns a member's full term history, but cd-lookup only
        # needs "who currently represents this district" -- so only the
        # current Congress's term is kept. (Senior/Junior Senator status --
        # issue #3 -- doesn't need this history either: see _crosswalk_row
        # below, sourced from a separate, editorially-maintained crosswalk
        # instead of derived from continuous-service math.)
        if term.congress != congress:
            continue

        chamber = CHAMBER_MAP[term.chamber]

        rows.append((
            bioguide_id,
            congress,
            chamber,
            term.member_type,
            term.state_code,
            term.district,
            term.start_year,
            term.end_year,
            congress_api.source_hash(
                bioguide_id, congress, chamber, term.member_type,
                term.state_code, term.district, term.start_year, term.end_year,
            ),
        ))

    return rows


def _crosswalk_row(entry: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    # legislators-current.yaml has no per-Congress "current term" marker
    # the way Congress.gov's own member detail does -- terms is instead
    # this person's full career history, so the current one is picked by
    # date: the entry whose [start, end] window contains today (a missing
    # end on the newest term is treated as open-ended, not excluded).
    # Ties aren't broken by trusting list order -- terms' ordering isn't
    # documented as guaranteed, same reasoning as _party_history's
    # explicit sort above -- the candidate with the latest start wins.
    try:
        bioguide_id = entry["id"]["bioguide"]
    except (KeyError, TypeError):
        return None

    today = date.today()
    current_term = None
    for term in entry.get("terms") or []:
        try:
            start = date.fromisoformat(term["start"])
            end_raw = term.get("end")
            end = date.fromisoformat(end_raw) if end_raw else None
        except (KeyError, TypeError, ValueError):
            # A malformed end (like a malformed start) should skip just
            # this one term, not raise out of _crosswalk_row entirely --
            # an uncaught exception here would propagate through
            # transform()'s crosswalk loop, which has no per-entry
            # try/except of its own, failing the whole task (including
            # the member/term transform it shares a task with).
            continue
        if start > today or (end is not None and today > end):
            continue
        if current_term is None or start > date.fromisoformat(current_term["start"]):
            current_term = term

    if current_term is None or current_term.get("type") != "sen":
        # House members (and anyone with no resolvable current term) get
        # no lis_member_id/senate_state_rank -- id.lis and terms[].state_rank
        # are Senate-only concepts in this source.
        return (bioguide_id, None, None)

    lis_member_id = entry["id"].get("lis")
    raw_rank = current_term.get("state_rank")
    # isinstance guard, not just truthiness -- raw_rank.upper() would raise
    # AttributeError uncaught on a non-string value, with nothing above
    # this function to catch it (same failure mode the start/end
    # date-parsing try/except above exists to avoid). Anything that
    # doesn't normalize to a real SENATE_STATE_RANKS value is dropped to
    # None rather than stored -- see SENATE_STATE_RANKS's own comment.
    normalized_rank = raw_rank.upper() if isinstance(raw_rank, str) else None
    senate_state_rank = normalized_rank if normalized_rank in SENATE_STATE_RANKS else None
    return (bioguide_id, lis_member_id, senate_state_rank)


@dag(
    dag_id="congress_members_etl",
    description="Sync House and Senate members of the current Congress into members/member_terms",
    schedule="@daily",
    start_date=datetime(2025, 1, 3),
    catchup=False,
    default_args={"retries": 2},
    tags=["congress"],
)
def congress_members_etl():

    @task
    def sync_current_congress() -> int:
        response = congress_api.api_get_model(
            _API_SESSION, CONGRESS_CURRENT_CONGRESS_API, CongressCurrentResponse,
        )
        number, start_date, end_date = _derive_congress_dates(response.congress)

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        hook.run(
            """
            INSERT INTO congresses (congress, start_date, end_date)
            VALUES (%s, %s, %s)
            ON CONFLICT (congress) DO UPDATE SET
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                updated_at = NOW()
            """,
            parameters=(number, start_date, end_date),
        )
        logger.info("Synced congress %d (%s to %s)", number, start_date, end_date)
        return number

    @task
    def get_current_congress(_synced_congress: int) -> int:
        # Determine "current" via the Postgres current_congress()
        # function (see init.sql) rather than the API's notion of
        # current, or re-typing the date-range predicate here -- that
        # function is the single place this ETL and the
        # current_members view both derive "current" from.
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        row = hook.get_first("SELECT current_congress()")
        if row is None or row[0] is None:
            raise ValueError("No current congress found in congresses table")
        return row[0]

    @task
    def extract_member_summaries(congress: int) -> list[dict[str, Any]]:
        # currentMember=false returns the full roster of this Congress,
        # including members who have since resigned, died, or been
        # expelled -- their term already carries an endYear from the
        # source. Filtering to currentMember=true would silently miss
        # that departure and leave end_year stuck at NULL forever.
        #
        # updateDate is included here (list-level, no extra calls) so
        # filter_members_needing_sync can skip the expensive per-member
        # detail call for anyone who hasn't changed since our last sync.
        #
        # Returned as plain dicts, not parsed MemberSummary models --
        # this crosses an Airflow XCom boundary (serialized to JSON),
        # which pydantic model instances can't survive any more than
        # psycopg2's Json wrapper can (see _wrap_party_history_for_insert's
        # comment). Validated instead at the point of use, in
        # _members_needing_sync.
        summaries = [
            {"bioguideId": item["bioguideId"], "updateDate": item.get("updateDate")}
            for item in congress_api.paginate(
                _API_SESSION,
                f"{CONGRESS_MEMBERS_API}congress/{congress}",
                {"currentMember": "false"},
                items_key="members",
                page_limit=PAGE_LIMIT,
            )
        ]

        logger.info("Found %d members of the %dth Congress", len(summaries), congress)
        return summaries

    @task
    def filter_members_needing_sync(
        summaries: list[dict[str, Any]], congress: int
    ) -> list[str]:
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        stored_updated_at = dict(
            hook.get_records("SELECT bioguide_id, source_updated_at FROM members")
        )
        bioguide_ids_with_current_term = {
            row[0]
            for row in hook.get_records(
                "SELECT DISTINCT bioguide_id FROM member_terms WHERE congress = %s",
                parameters=(congress,),
            )
        }

        stale_or_new = _members_needing_sync(
            summaries, stored_updated_at, bioguide_ids_with_current_term,
        )

        logger.info(
            "%d of %d members need a detail sync", len(stale_or_new), len(summaries),
        )
        return stale_or_new

    @task
    def fetch_member_details(bioguide_ids: list[str]) -> list[dict[str, Any]]:
        def fetch_one(bioguide_id: str) -> dict[str, Any]:
            return congress_api.api_get(
                _API_SESSION, f"{CONGRESS_MEMBERS_API}{bioguide_id}",
            )["member"]

        details = congress_api.fetch_concurrently(
            bioguide_ids, fetch_one, DETAIL_FETCH_WORKERS,
        )

        logger.info(
            "Fetched details for %d of %d members", len(details), len(bioguide_ids),
        )
        return details

    @task
    def extract_legislators_crosswalk() -> list[dict[str, Any]]:
        # Independent of current_congress/member details -- Airflow runs
        # this concurrently with the rest of the chain above. Deliberately
        # never raises: a broken/unreachable crosswalk source degrades to
        # "no crosswalk update today" (load() below just gets an empty
        # list), not a failed/retried run of the member sync cd-lookup
        # actually depends on.
        try:
            response = _API_SESSION.get(LEGISLATORS_CROSSWALK_URL, timeout=30)
            response.raise_for_status()
            legislators = yaml.safe_load(response.text) or []
        except Exception as exc:
            logger.error("Skipping legislators crosswalk sync: %s", exc)
            return []

        logger.info("Fetched %d legislators for the LIS/seniority crosswalk", len(legislators))
        return legislators

    @task
    def transform(
        members: list[dict[str, Any]], congress: int, crosswalk_raw: list[dict[str, Any]],
    ) -> dict[str, list[tuple[Any, ...]]]:
        member_rows = []
        term_rows = []
        dropped_party_history_count = 0

        for member in members:
            try:
                # Validated here, immediately before use, rather than in
                # fetch_member_details -- that task's return value
                # crosses an Airflow XCom boundary (serialized to JSON),
                # which a pydantic model instance can't survive, so it
                # has to stay a plain dict until a task that doesn't
                # need to hand its result off can parse it. This is
                # still "at the boundary" in the sense issue #7 means:
                # one clear, well-located ValidationError naming the
                # exact field, instead of an obscure crash several
                # functions later inside _member_row/_term_rows.
                parsed_member = MemberDetail.model_validate(member)
                member_row = _member_row(parsed_member)
                member_term_rows = _term_rows(parsed_member, congress)
            except (KeyError, TypeError, ValidationError) as exc:
                # One member with unexpected/missing API fields (e.g. an
                # unrecognized chamber value) shouldn't abort the whole
                # batch -- log it and continue with everyone else.
                logger.error(
                    "Skipping member %s: malformed API data (%s)",
                    member.get("bioguideId", "<unknown>"), exc,
                )
                continue

            dropped_party_history_count += _count_missing_start_year(
                parsed_member.party_history
            )
            member_rows.append(member_row)
            term_rows.extend(member_term_rows)

        crosswalk_rows = []
        dropped_crosswalk_count = 0
        for entry in crosswalk_raw:
            row = _crosswalk_row(entry)
            if row is None:
                dropped_crosswalk_count += 1
                continue
            crosswalk_rows.append(row)

        logger.info(
            "Transformed %d members into %d term rows "
            "(%d party_history entries dropped for missing start_year), "
            "%d crosswalk rows (%d dropped for missing bioguide_id)",
            len(member_rows), len(term_rows), dropped_party_history_count,
            len(crosswalk_rows), dropped_crosswalk_count,
        )
        return {"members": member_rows, "terms": term_rows, "crosswalk": crosswalk_rows}

    @task
    def load(rows: dict[str, list[tuple[Any, ...]]]) -> None:
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()

        member_rows = _wrap_party_history_for_insert(rows["members"])

        try:
            with conn.cursor() as cursor:
                execute_values(cursor, MEMBERS_UPSERT_SQL, member_rows)
                execute_values(cursor, MEMBER_TERMS_UPSERT_SQL, rows["terms"])

            conn.commit()
            logger.info(
                "Loaded %d members and %d terms",
                len(rows["members"]), len(rows["terms"]),
            )
        finally:
            # No explicit rollback needed on the exception path: conn is
            # a fresh, never-reused connection (hook.get_conn() opens a
            # new one each call), and psycopg2 performs an implicit
            # rollback when a connection is closed without a prior
            # commit -- so close() here already discards any
            # uncommitted work.
            conn.close()

    @task
    def load_crosswalk(rows: dict[str, list[tuple[Any, ...]]]) -> None:
        # A separate @task, not folded into load() above, specifically so
        # a crosswalk-specific failure gets Airflow's own task-level
        # retries (default_args={"retries": 2}) and shows up as a failed
        # task run, instead of being caught and swallowed into one log
        # line the way a hand-rolled try/except inside load() would --
        # unlike that, an uncaught exception here is allowed to propagate.
        # This still can't block or roll back the member/term sync: this
        # task never receives load()'s connection or touches its data,
        # and the DAG wiring below makes it strictly downstream of load()
        # already having committed, not just downstream of transform().
        crosswalk_rows = rows["crosswalk"]
        if not crosswalk_rows:
            logger.info("No crosswalk rows to update")
            return

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()
        try:
            with conn.cursor() as cursor:
                execute_values(cursor, LEGISLATORS_CROSSWALK_UPDATE_SQL, crosswalk_rows)
            conn.commit()
            logger.info("Updated LIS/seniority crosswalk for %d members", len(crosswalk_rows))
        finally:
            conn.close()

    current_congress = get_current_congress(sync_current_congress())
    summaries = extract_member_summaries(current_congress)
    member_ids = filter_members_needing_sync(summaries, current_congress)
    details = fetch_member_details(member_ids)
    crosswalk_raw = extract_legislators_crosswalk()
    transformed = transform(details, current_congress, crosswalk_raw)
    load(transformed) >> load_crosswalk(transformed)


congress_members_etl()
