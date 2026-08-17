from __future__ import annotations

from typing import Any


def _person(row: dict[str, Any]) -> dict[str, Any]:
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
        "senators": [_person(row) for row in rows if row["chamber"] == "SENATE"],
        "representatives": [_person(row) for row in rows if row["chamber"] == "HOUSE"],
    }
