from __future__ import annotations

from typing import Any


def _full_name(row: dict[str, Any]) -> str:
    parts = [row.get("given_name"), row.get("middle_name"), row.get("family_name")]
    name = " ".join(part for part in parts if part)
    return f"{name} {row['suffix']}" if row.get("suffix") else name


def _person(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": _full_name(row),
        "role": "Senator" if row["chamber"] == "SENATE" else "Representative",
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
