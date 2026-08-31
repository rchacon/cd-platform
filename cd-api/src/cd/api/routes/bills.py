from __future__ import annotations

import logging

from botocore.config import Config
from fastapi import APIRouter, HTTPException, Query

from cd.api.db import (
    fetch_bills_by_policy_area,
    fetch_bills_by_similarity,
    fetch_bills_by_subject,
    fetch_closest_vocab_term,
)
from cd.api.jsonapi import JsonApiResponse, JsonApiRoute
from cd.api.openapi import jsonapi_error_response
from cd.api.transform import bill_search_document
from cd.lib import bedrock
from cd.lib.jsonapi import CollectionDocument
from cd.lib.models import Bill

logger = logging.getLogger(__name__)

# Built once at import time (Lambda cold start), same precedent as
# cd-etl's DAG modules.
#
# The Config bounds the whole embed call -- every retry, both phases --
# inside the Lambda's 25s function timeout. botocore's own defaults (60s
# connect, 60s read) outlast it, so when the network path to Bedrock is
# broken -- e.g. the Lambda's SG missing a 443 egress rule, exactly how
# GET /bills first shipped -- invoke_model() would hang past the
# timeout and the sandbox is killed mid-call, surfacing as an uncatchable
# 500 rather than reaching the except -> 503 below. total_max_attempts is
# max_attempts + 1 == 2, a single attempt's worst case is
# connect_timeout + read_timeout == 10s, so the ceiling is ~21s (2*10s +
# one standard-mode backoff), leaving room for the handler's own DB round
# trips. A healthy embed of a <=500-char query returns well under 1s, so
# 5s read is generous; 5s connect is the conventional AWS-SDK floor and
# tolerates a cold VPC Lambda's first TLS-through-NAT handshake. More real
# headroom needs the Lambda's own timeout/memory raised (cd-infra#58).
_BEDROCK_CLIENT = bedrock.build_bedrock_client(
    Config(
        connect_timeout=5,
        read_timeout=5,
        retries={"max_attempts": 1, "mode": "standard"},
    )
)

# A query embedding within this cosine distance of the closest vocab term
# is treated as a confident tier-1 match (exact policy_area/subject_name
# lookup); anything farther falls through to tier-2 similarity search
# over bills.crs_summary_embedding instead. Placeholder -- tune
# empirically once real query traffic exists.
VOCAB_MATCH_THRESHOLD = 0.25

# Relevance floor for tier-2 similarity search: a bill farther than this
# from the query embedding is treated as "not actually about this topic"
# and excluded, rather than backfilled in just to pad the response out
# to `limit`. Unlike VOCAB_MATCH_THRESHOLD (a pure guess), this was
# calibrated against real Titan V2 embeddings of real synced bills:
# genuinely on-topic matches clustered at ~0.72-0.78 cosine distance
# across several test queries, while a query with no genuinely related
# bill in the corpus only produced matches at 0.87+ -- 0.80 sits
# cleanly in the gap between the two. Still worth re-tuning once real
# query traffic and a full-size corpus (a few hundred bills, not 61)
# exist.
BILL_SIMILARITY_THRESHOLD = 0.80

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

# One router -- the only bills route speaks JSON:API, so unlike
# routes/members.py there's no bespoke sibling to keep separate.
router = APIRouter(route_class=JsonApiRoute, tags=["bills"])


@router.get(
    "/bills",
    response_model=CollectionDocument[Bill],
    response_class=JsonApiResponse,
    # `bill` resources carry no relationships; exclude_none drops the
    # wrapper's default `None` so the response omits the member rather
    # than emitting `"relationships": null` (invalid per JSON:API). It
    # also omits a null `title`/`policy_area`/`crs_summary` -- absent,
    # not null, which JSON:API treats the same. Same call as
    # GET /members/{bioguide_id}.
    response_model_exclude_none=True,
    responses={
        400: jsonapi_error_response("An unsupported query parameter was sent."),
        405: jsonapi_error_response("HTTP method not allowed for this path."),
        406: jsonapi_error_response(
            "`Accept` offers the JSON:API media type only with a media-type "
            "parameter other than `profile`/`ext`."
        ),
        415: jsonapi_error_response(
            "`Content-Type` is the JSON:API media type with a media-type "
            "parameter other than `profile`/`ext`."
        ),
        422: jsonapi_error_response(
            "Request parameters failed validation (e.g. `filter[query]` "
            f"missing/empty/over 500 chars, or `page[size]` outside 1..{MAX_PAGE_SIZE})."
        ),
        500: jsonapi_error_response("An unexpected error occurred."),
        503: jsonapi_error_response(
            "Search is temporarily unavailable (embedding generation failed)."
        ),
    },
)
def get_bills(
    query: str = Query(
        ...,
        alias="filter[query]",
        min_length=1,
        max_length=500,
        description=(
            "Free-text topic to search for -- a JSON:API filter narrowing "
            "the bill collection to what's about this topic."
        ),
    ),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        alias="page[size]",
        ge=1,
        le=MAX_PAGE_SIZE,
        description=(
            f"Maximum bills to return, 1..{MAX_PAGE_SIZE} (default "
            f"{DEFAULT_PAGE_SIZE}). No offset/cursor pagination yet -- a "
            "single page, capped."
        ),
    ),
) -> dict:
    """Semantic search over bills.

    Matches `filter[query]` against a bill's policy area or legislative
    subjects first (tier 1, exact controlled-vocabulary match against
    the closest embedding in `vocab_term_embeddings`); any remaining
    slots up to `page[size]` are then considered for tier-2
    cosine-similarity search directly against each bill's own summary
    embedding, subject to `BILL_SIMILARITY_THRESHOLD` -- a bill farther
    than that is excluded rather than backfilled in, so this can return
    fewer than `page[size]` (even zero) bills when nothing in the corpus
    is genuinely close enough.

    Returns a JSON:API collection of `bill` resources -- `{"data":
    [{"type": "bill", "id": "119-hr-2616", "attributes": {"congress",
    "bill_type", "bill_number", "title", "policy_area", "crs_summary"},
    "meta": {"match": "policy_area"}}, ...], "meta": {"query": "..."}}`
    -- in retrieval-tier order. Each resource's `meta.match`
    (`policy_area` / `subject` / `similarity`) says which tier surfaced
    that bill -- per-resource `meta`, not an attribute, since it's about
    this search, not the bill. The resource `id` is the canonical
    `bills.bill_key`, which a caller passes to
    `GET /members/{bioguide_id}/votes`'s `filter[bill]` to get a
    member's votes on these bills. Side-effect-free and cacheable on the
    query alone.
    """
    try:
        query_embedding = bedrock.embed(_BEDROCK_CLIENT, query)
    except Exception as exc:
        # Unlike cd-etl's degrade-gracefully precedent for a stale
        # embedding, there's nothing to fall back to here -- the
        # embedding IS the query. 503 signals retryability more usefully
        # than the generic 500 catch-all below.
        logger.exception("Bedrock embed failed for query %r", query)
        raise HTTPException(
            status_code=503, detail="Search is temporarily unavailable."
        ) from exc

    vocab_match = fetch_closest_vocab_term(query_embedding)
    tier1_bills: list[dict] = []
    if vocab_match is not None and vocab_match["distance"] <= VOCAB_MATCH_THRESHOLD:
        if vocab_match["kind"] == "POLICY_AREA":
            tier1_bills = fetch_bills_by_policy_area(vocab_match["term"], page_size)
            tier1_match = "policy_area"
        else:
            tier1_bills = fetch_bills_by_subject(vocab_match["term"], page_size)
            tier1_match = "subject"
        for bill in tier1_bills:
            bill["match"] = tier1_match

    remaining = page_size - len(tier1_bills)
    tier2_bills = (
        fetch_bills_by_similarity(
            query_embedding, [b["bill_id"] for b in tier1_bills], remaining,
            BILL_SIMILARITY_THRESHOLD,
        )
        if remaining > 0
        else []
    )
    for bill in tier2_bills:
        bill["match"] = "similarity"

    return bill_search_document(query, tier1_bills + tier2_bills)
