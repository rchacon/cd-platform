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


def _party_history(party_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "party": PARTY_MAP.get(entry.get("partyName"), "OTHER"),
            "source_party_name": entry.get("partyName"),
            "start_year": _to_smallint(entry.get("startYear")),
            "end_year": _to_smallint(entry.get("endYear")),
        }
        for entry in party_history
    ]


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
        Json(party_history),
        _source_hash(
            bioguide_id, given_name, middle_name, family_name, nickname,
            suffix, birth_year, death_year, photo_uri, phone, website_url,
            party_history,
        ),
        source_updated_at,
    )


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
        # seats (unlike the list endpoint, which returns 0), so a
        # missing value for a House seat means at-large, not Senate.
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
        # The API's own "endYear" is a generalized label (off by one from
        # the actual term-end date), so the real end date is derived as
        # exactly two years after the earliest session start date -- the
        # same convention init.sql used to seed the 119th Congress.
        payload = _api_get(CONGRESS_CURRENT_CONGRESS_API)["congress"]
        number = payload["number"]
        start_year = int(payload["startYear"])
        start_date = min(
            (
                date.fromisoformat(session["startDate"])
                for session in payload["sessions"]
                if "startDate" in session
            ),
            default=date(start_year, 1, 3),
        )
        end_date = date(start_year + 2, 1, 3)

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
    def extract_member_ids(congress: int) -> list[str]:
        # currentMember=false returns the full roster of this Congress,
        # including members who have since resigned, died, or been
        # expelled -- their term already carries an endYear from the
        # source. Filtering to currentMember=true would silently miss
        # that departure and leave end_year stuck at NULL forever.
        bioguide_ids = []
        offset = 0

        while True:
            page = _api_get(
                f"{CONGRESS_MEMBERS_API}congress/{congress}",
                {"currentMember": "false", "limit": PAGE_LIMIT, "offset": offset},
            )
            members = page.get("members", [])
            if not members:
                break

            bioguide_ids.extend(member["bioguideId"] for member in members)

            if len(members) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

        logger.info("Found %d members of the %dth Congress", len(bioguide_ids), congress)
        return bioguide_ids

    @task
    def fetch_member_details(bioguide_ids: list[str]) -> list[dict[str, Any]]:
        def fetch_one(bioguide_id: str) -> dict[str, Any]:
            return _api_get(f"{CONGRESS_MEMBERS_API}{bioguide_id}")["member"]

        with ThreadPoolExecutor(max_workers=DETAIL_FETCH_WORKERS) as executor:
            details = list(executor.map(fetch_one, bioguide_ids))

        logger.info("Fetched details for %d members", len(details))
        return details

    @task
    def transform(
        members: list[dict[str, Any]], congress: int
    ) -> dict[str, list[tuple[Any, ...]]]:
        member_rows = [_member_row(member) for member in members]
        term_rows = [row for member in members for row in _term_rows(member, congress)]

        logger.info(
            "Transformed %d members into %d term rows",
            len(member_rows), len(term_rows),
        )
        return {"members": member_rows, "terms": term_rows}

    @task
    def load(rows: dict[str, list[tuple[Any, ...]]]) -> None:
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        conn = hook.get_conn()

        try:
            with conn.cursor() as cursor:
                execute_values(
                    cursor,
                    """
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
                        updated_at = NOW()
                    """,
                    rows["members"],
                )

                execute_values(
                    cursor,
                    """
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
                    """,
                    rows["terms"],
                )

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
    member_ids = extract_member_ids(current_congress)
    details = fetch_member_details(member_ids)
    load(transform(details, current_congress))


congress_members_etl()
