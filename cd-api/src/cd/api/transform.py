from __future__ import annotations

from typing import Any


def _person(row: dict[str, Any]) -> dict[str, Any]:
    return {
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
    }


def group_representatives(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "senators": [_person(row) for row in rows if row["chamber"] == "SENATE"],
        "representatives": [_person(row) for row in rows if row["chamber"] == "HOUSE"],
    }
