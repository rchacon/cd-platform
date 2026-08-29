from __future__ import annotations

from typing import Any


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
