from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

import requests
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from psycopg2.extras import Json, execute_values

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_MEMBERS_API = "https://api.congress.gov/v3/member/"
CONGRESS_CURRENT_CONGRESS_API = "https://api.congress.gov/v3/congress/current"

PAGE_LIMIT = 250
DETAIL_FETCH_WORKERS = 10
POSTGRES_CONN_ID = "congressional_postgres"

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
    "Libertarian": "LIBERTARIAN",
    "Green": "GREEN",
}

logger = logging.getLogger(__name__)


def _api_get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(
        url,
        params={**(params or {}), "api_key": CONGRESS_API_KEY, "format": "json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _to_smallint(value: str | int | None) -> int | None:
    return int(value) if value is not None else None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _derive_congress_dates(payload: dict[str, Any]) -> tuple[int, date, date]:
    # The API's own "endYear" is a generalized label (off by one from
    # the actual term-end date), so the real end date is derived as
    # exactly two years after the earliest session start date -- the
    # same convention init.sql used to seed the 119th Congress.
    number = payload["number"]
    start_year = int(payload["startYear"])
    start_date = min(
        (
            date.fromisoformat(session["startDate"])
            for session in payload.get("sessions", [])
            if "startDate" in session
        ),
        default=date(start_year, 1, 3),
    )
    end_date = date(start_year + 2, 1, 3)
    return number, start_date, end_date


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
    stale_or_new = []
    for summary in summaries:
        bioguide_id = summary["bioguideId"]
        last_synced = stored_updated_at.get(bioguide_id)
        source_updated = _parse_timestamp(summary.get("updateDate"))

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


def _count_missing_start_year(party_history: list[dict[str, Any]]) -> int:
    return sum(1 for entry in party_history if entry.get("startYear") is None)


def _party_history(party_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # An entry with no startYear can't be placed chronologically, so it
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
                "party": PARTY_MAP.get(entry.get("partyName"), "OTHER"),
                "source_party_name": entry.get("partyName"),
                "start_year": _to_smallint(entry.get("startYear")),
                "end_year": _to_smallint(entry.get("endYear")),
            }
            for entry in party_history
            if entry.get("startYear") is not None
        ),
        key=lambda period: period["start_year"],
    )


def _source_hash(*parts: Any) -> str:
    normalized = "|".join(
        str(part).strip().lower() if part is not None else "" for part in parts
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _member_row(member: dict[str, Any]) -> tuple[Any, ...]:
    bioguide_id = member["bioguideId"]
    given_name = member.get("firstName")
    middle_name = member.get("middleName")
    family_name = member.get("lastName")
    nickname = member.get("nickName")
    suffix = member.get("suffixName")
    birth_year = _to_smallint(member.get("birthYear"))
    death_year = _to_smallint(member.get("deathYear"))
    photo_uri = member.get("depiction", {}).get("imageUrl")
    address_info = member.get("addressInformation", {})
    phone = address_info.get("phoneNumber")
    website_url = member.get("officialWebsiteUrl")
    party_history = _party_history(member.get("partyHistory", []))
    source_updated_at = _parse_timestamp(member.get("updateDate"))

    return (
        bioguide_id,
        given_name,
        middle_name,
        family_name,
        nickname,
        suffix,
        birth_year,
        death_year,
        photo_uri,
        phone,
        website_url,
        party_history,
        _source_hash(
            bioguide_id, given_name, middle_name, family_name, nickname,
            suffix, birth_year, death_year, photo_uri, phone, website_url,
            party_history,
        ),
        source_updated_at,
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


def _term_rows(member: dict[str, Any], congress: int) -> list[tuple[Any, ...]]:
    bioguide_id = member["bioguideId"]

    rows = []
    for term in member.get("terms", []):
        if term.get("congress") != congress:
            continue

        chamber = CHAMBER_MAP[term["chamber"]]
        member_type = term["memberType"]
        state = term["stateCode"]
        # The item-level API omits "district" entirely for at-large
        # seats (unlike the list endpoint, which returns 0) -- confirmed
        # directly against the live API (e.g. M001238/McBride, at-large
        # DE: item-level omits district, list-level shows district: 0;
        # same pattern for DC/territory delegates). This is a
        # deliberate, verified API convention, not a guess, so a
        # missing value for a House seat is treated as at-large.
        district = (term.get("district") or 0) if chamber == "HOUSE" else None
        start_year = _to_smallint(term.get("startYear"))
        end_year = _to_smallint(term.get("endYear"))

        rows.append((
            bioguide_id,
            congress,
            chamber,
            member_type,
            state,
            district,
            start_year,
            end_year,
            _source_hash(
                bioguide_id, congress, chamber, member_type, state,
                district, start_year, end_year,
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
        payload = _api_get(CONGRESS_CURRENT_CONGRESS_API)["congress"]
        number, start_date, end_date = _derive_congress_dates(payload)

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
        # Determine "current" from our own congresses table rather than
        # the API's notion of current, so this ETL and the
        # current_member_terms view share a single definition.
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        row = hook.get_first(
            """
            SELECT congress FROM congresses
            WHERE start_date <= CURRENT_DATE AND CURRENT_DATE < end_date
            ORDER BY congress DESC
            LIMIT 1
            """
        )
        if row is None:
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
        summaries = []
        offset = 0

        while True:
            page = _api_get(
                f"{CONGRESS_MEMBERS_API}congress/{congress}",
                {"currentMember": "false", "limit": PAGE_LIMIT, "offset": offset},
            )
            members = page.get("members", [])
            if not members:
                break

            summaries.extend(
                {"bioguideId": member["bioguideId"], "updateDate": member.get("updateDate")}
                for member in members
            )

            if len(members) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

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
            return _api_get(f"{CONGRESS_MEMBERS_API}{bioguide_id}")["member"]

        details = []
        with ThreadPoolExecutor(max_workers=DETAIL_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(fetch_one, bioguide_id): bioguide_id
                for bioguide_id in bioguide_ids
            }
            for future in futures:
                try:
                    details.append(future.result())
                except Exception as exc:
                    # One member's detail fetch failing (404, rate
                    # limit, transient 5xx) shouldn't discard every
                    # other already-fetched member in this batch.
                    logger.error(
                        "Failed to fetch detail for %s: %s", futures[future], exc,
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
                member_row = _member_row(member)
                member_term_rows = _term_rows(member, congress)
            except (KeyError, TypeError) as exc:
                # One member with unexpected/missing API fields (e.g. an
                # unrecognized chamber value) shouldn't abort the whole
                # batch -- log it and continue with everyone else.
                logger.error(
                    "Skipping member %s: malformed API data (%s)",
                    member.get("bioguideId", "<unknown>"), exc,
                )
                continue

            dropped_party_history_count += _count_missing_start_year(
                member.get("partyHistory", [])
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
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    current_congress = get_current_congress(sync_current_congress())
    summaries = extract_member_summaries(current_congress)
    member_ids = filter_members_needing_sync(summaries, current_congress)
    details = fetch_member_details(member_ids)
    load(transform(details, current_congress))


congress_members_etl()
