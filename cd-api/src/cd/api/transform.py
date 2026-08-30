from __future__ import annotations

from typing import Any


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
