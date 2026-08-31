from __future__ import annotations

from typing import Any

# DB row(s) -> API response dict. `person`/`group_representatives` shape
# current_members rows for GET /members (bespoke {senators,
# representatives} shape); `member_document` shapes one current_members
# row into the JSON:API single-resource document GET /members/{bioguide_id}
# now returns; `shape_member_votes` shapes fetch_member_votes rows into
# the JSON:API `roll_call_vote` collection GET /members/{bioguide_id}/votes
# returns; `shape_bill_search_response` shapes bills + votes rows for
# GET /bills/search (still bespoke -- reshaped in a later PR).


def _roll_call_id(row: dict[str, Any]) -> str:
    # The canonical roll_call id: "<congress>-<chamber>-<session>-<vote_number>",
    # e.g. "119-house-1-327". Built from roll_calls' NOT NULL unique
    # (chamber, congress, session, vote_number) natural key -- same
    # single-source-of-truth idea as bills.bill_key, just assembled here
    # rather than in a generated column (yet).
    return (
        f"{row['congress']}-{row['chamber'].lower()}-"
        f"{row['session']}-{row['vote_number']}"
    )


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


def member_document(row: dict[str, Any]) -> dict[str, Any]:
    # GET /members/{bioguide_id}'s JSON:API single-resource document:
    # {"data": {"type": "member", "id": "<bioguide_id>", "attributes":
    # {...}}}. Identity moves to the resource `id`, so `attributes` is
    # `person(row)` minus bioguide_id, plus the two fields this endpoint
    # carries over GET /members' shape (state, in_office).
    attributes = {k: v for k, v in person(row).items() if k != "bioguide_id"}
    attributes["state"] = row["state"]
    attributes["in_office"] = row["in_office"]
    return {
        "data": {
            "type": "member",
            "id": row["bioguide_id"],
            "attributes": attributes,
        }
    }


def shape_member_votes(
    vote_rows: list[dict[str, Any]],
    bioguide_id: str,
    requested_bill_keys: list[str],
) -> dict[str, Any]:
    # GET /members/{bioguide_id}/votes' JSON:API collection of
    # `roll_call_vote` resources -- one per roll call this member cast,
    # each linking (relationships, linkage only) to the `member`, the
    # `roll_call`, and the `bill` it was a vote on. The `bill` edge is a
    # denormalised convenience (structurally a roll_call_vote reaches a
    # bill *via* its roll_call) -- carried directly so a caller can group
    # votes by bill without fetching the roll_call. vote_question/result/
    # vote_date are likewise denormalised from the roll call: there's no
    # `included`, so the fields a client needs to render a vote ride
    # along on the vote itself.
    #
    # Ordering: `data` follows the caller's requested bill order; within
    # a bill, oldest vote first (fetch_member_votes' ORDER BY).
    #
    # Three cases for a requested bill_key:
    #   - matched rows with a cast vote  -> one resource per vote
    #   - matched rows, no cast vote     -> id in meta.bills_without_votes
    #     (synced bill, member just never had a floor vote on it)
    #   - matched no row at all          -> absent entirely (not a synced
    #     bill) -- distinguishable from "no vote" by being in neither
    #     `data` nor `meta`.
    votes_by_bill: dict[str, list[dict[str, Any]]] = {}
    bills_seen: set[str] = set()
    for row in vote_rows:
        bill_key = row["bill_key"]
        bills_seen.add(bill_key)
        if row["vote_cast"] is None:
            continue
        roll_call_id = _roll_call_id(row)
        votes_by_bill.setdefault(bill_key, []).append({
            "type": "roll_call_vote",
            "id": f"{roll_call_id}:{bioguide_id}",
            "attributes": {
                "vote_cast": row["vote_cast"],
                "vote_question": row["vote_question"],
                "result": row["result"],
                "vote_date": row["vote_date"],
            },
            "relationships": {
                "member": {"data": {"type": "member", "id": bioguide_id}},
                "roll_call": {"data": {"type": "roll_call", "id": roll_call_id}},
                "bill": {"data": {"type": "bill", "id": bill_key}},
            },
        })

    data = [
        resource
        for key in requested_bill_keys
        for resource in votes_by_bill.get(key, [])
    ]
    bills_without_votes = [
        key
        for key in requested_bill_keys
        if key in bills_seen and key not in votes_by_bill
    ]
    return {"data": data, "meta": {"bills_without_votes": bills_without_votes}}


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
