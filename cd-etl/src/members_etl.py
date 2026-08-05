from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import congress_api
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from psycopg2.extras import Json, execute_values
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic.alias_generators import to_camel

CONGRESS_MEMBERS_API = "https://api.congress.gov/v3/member/"
CONGRESS_CURRENT_CONGRESS_API = "https://api.congress.gov/v3/congress/current"

PAGE_LIMIT = 250
DETAIL_FETCH_WORKERS = 10
POSTGRES_CONN_ID = "congressional_postgres"

_API_SESSION = congress_api.build_session(pool_maxsize=DETAIL_FETCH_WORKERS)


class _CamelModel(BaseModel):
    # Congress.gov's JSON uses camelCase keys throughout; fields here
    # are named idiomatically in snake_case, with the alias generator
    # mapping each one onto its camelCase wire name automatically (e.g.
    # bioguide_id <-> bioguideId). populate_by_name also allows
    # constructing instances directly via the snake_case field names
    # (used by tests), not just via model_validate() against raw JSON.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PartyHistoryEntry(_CamelModel):
    party_name: str | None = None
    start_year: int | None = None
    end_year: int | None = None


class Term(_CamelModel):
    chamber: str
    congress: int
    member_type: str
    state_code: str
    district: int | None = None
    start_year: int | None = None
    end_year: int | None = None

    @model_validator(mode="after")
    def _normalize_house_district(self) -> "Term":
        # The item-level API omits "district" entirely for at-large
        # seats (unlike the list endpoint, which returns 0) -- confirmed
        # directly against the live API (e.g. M001238/McBride, at-large
        # DE: item-level omits district, list-level shows district: 0;
        # same pattern for DC/territory delegates). A missing value for
        # a House seat means at-large, not unknown. Senate seats have no
        # district at all, regardless of what the source sends.
        if self.chamber == "House of Representatives":
            if self.district is None:
                self.district = 0
        else:
            self.district = None
        return self


class Depiction(_CamelModel):
    image_url: str | None = None


class AddressInformation(_CamelModel):
    phone_number: str | None = None


class MemberSummary(_CamelModel):
    bioguide_id: str
    update_date: datetime | None = None


class MemberDetail(_CamelModel):
    bioguide_id: str
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    nick_name: str | None = None
    suffix_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    depiction: Depiction = Depiction()
    address_information: AddressInformation = AddressInformation()
    official_website_url: str | None = None
    party_history: list[PartyHistoryEntry] = []
    update_date: datetime | None = None
    terms: list[Term] = []


class CongressSession(_CamelModel):
    start_date: date | None = None


class CongressCurrent(_CamelModel):
    number: int
    start_year: int
    sessions: list[CongressSession] = []


class CongressCurrentResponse(_CamelModel):
    congress: CongressCurrent


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
        # current Congress's term is kept. This is why current_members
        # can't derive Senior/Junior Senator status (see issue #3): that
        # requires continuous-service history this deliberately discards.
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
    def transform(
        members: list[dict[str, Any]], congress: int
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

        logger.info(
            "Transformed %d members into %d term rows "
            "(%d party_history entries dropped for missing start_year)",
            len(member_rows), len(term_rows), dropped_party_history_count,
        )
        return {"members": member_rows, "terms": term_rows}

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

    current_congress = get_current_congress(sync_current_congress())
    summaries = extract_member_summaries(current_congress)
    member_ids = filter_members_needing_sync(summaries, current_congress)
    details = fetch_member_details(member_ids)
    load(transform(details, current_congress))


congress_members_etl()
