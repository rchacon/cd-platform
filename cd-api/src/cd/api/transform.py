from __future__ import annotations

from typing import Any

# DB row(s) -> API response dict. `person`/`group_representatives` shape
# current_members rows for GET /members[/{bioguide_id}];
# `shape_bill_search_response` shapes bills + votes rows for
# GET /bills/search.


def person(row: dict[str, Any]) -> dict[str, Any]:
    # The Member shape shared by GET /members (grouped by chamber, below)
    # and GET /members/{bioguide_id} (which adds `state` on top).
    return {
        "bioguide_id": row["bioguide_id"],
        "first_name": row.get("given_name"),
        "middle_name": row.get("middle_name"),
        "last_name": row.get("family_name"),
        "nickname": row.get("nickname"),
        "suffix": row.get("suffix"),
        "role": row["member_type"],
        "party": row.get("party"),
        "phone": row.get("phone"),
        "website": row.get("website_url"),
        "photo_url": row.get("photo_uri"),
        # NULL for a Senator, 0 for an at-large House seat, 1+ for a
        # numbered House district -- member_terms.district's own
        # NULL/0/1+ convention (see cd-etl/migrations' initial schema),
        # passed straight through.
        "district": row.get("district"),
    }


def group_representatives(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "senators": [person(row) for row in rows if row["chamber"] == "SENATE"],
        "representatives": [person(row) for row in rows if row["chamber"] == "HOUSE"],
    }


def shape_bill_search_response(
    query: str,
    bioguide_id: str,
    bill_rows: list[dict[str, Any]],
    vote_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    votes_by_bill_id: dict[int, list[dict[str, Any]]] = {}
    for row in vote_rows:
        votes_by_bill_id.setdefault(row["bill_id"], []).append({
            "vote_cast": row["vote_cast"],
            "vote_question": row["vote_question"],
            "result": row["result"],
            "vote_date": row["vote_date"],
        })

    return {
        "query": query,
        "bioguide_id": bioguide_id,
        "bills": [
            {
                "id": row["bill_key"],
                "congress": row["congress"],
                "bill_type": row["bill_type"],
                "bill_number": row["bill_number"],
                "title": row.get("title"),
                "policy_area": row.get("policy_area"),
                "crs_summary": row.get("crs_summary"),
                "votes": votes_by_bill_id.get(row["bill_id"], []),
            }
            for row in bill_rows
        ],
    }
