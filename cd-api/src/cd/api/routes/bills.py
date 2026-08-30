from __future__ import annotations

import logging

from botocore.config import Config
from fastapi import APIRouter, HTTPException, Query

from cd.api.db import (
    fetch_bills_by_policy_area,
    fetch_bills_by_similarity,
    fetch_bills_by_subject,
    fetch_closest_vocab_term,
    fetch_votes_for_bills,
    member_exists,
)
from cd.api.openapi import error_response
from cd.api.transform import shape_bill_search_response
from cd.lib import bedrock
from cd.lib.models import BillSearchResponse

logger = logging.getLogger(__name__)

# Built once at import time (Lambda cold start), same precedent as
# cd-etl's DAG modules.
#
# The Config bounds the whole embed call -- every retry, both phases --
# inside the Lambda's 25s function timeout. botocore's own defaults (60s
# connect, 60s read) outlast it, so when the network path to Bedrock is
# broken -- e.g. the Lambda's SG missing a 443 egress rule, exactly how
# GET /bills/search first shipped -- invoke_model() would hang past the
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

router = APIRouter(tags=["bills"])


@router.get(
    "/bills/search",
    response_model=BillSearchResponse,
    responses={
        404: error_response("Unknown bioguide_id.", "ProblemDetail"),
        405: error_response("HTTP method not allowed for this path.", "ProblemDetail"),
        422: error_response(
            "Request parameters failed validation.", "ValidationProblemDetail"
        ),
        500: error_response("An unexpected error occurred.", "ProblemDetail"),
        503: error_response(
            "Search is temporarily unavailable (embedding generation failed).",
            "ProblemDetail",
        ),
    },
)
def get_bills_search(
    q: str = Query(..., min_length=1, max_length=500, description="Free-text search query."),
    bioguide_id: str = Query(
        ..., description="Scopes results to how this specific representative voted."
    ),
    limit: int = Query(10, ge=1, le=50),
) -> dict:
    """Semantic search over bills, scoped to one representative's votes.

    Matches `q` against a bill's policy area or legislative subjects
    first (tier 1, exact controlled-vocabulary match against the
    closest embedding in `vocab_term_embeddings`); any remaining slots
    up to `limit` are then considered for tier-2 cosine-similarity
    search directly against each bill's own summary embedding, subject
    to `BILL_SIMILARITY_THRESHOLD` -- a bill farther than that from `q`
    is excluded rather than backfilled in, so this can return fewer
    than `limit` (even zero) bills when nothing in the corpus is
    genuinely close enough. Each returned bill includes every roll call
    `bioguide_id` cast in their own chamber for it (empty if the bill
    matched but they never voted on it).
    """
    if not member_exists(bioguide_id):
        raise HTTPException(status_code=404, detail=f"Unknown bioguide_id {bioguide_id}")

    try:
        query_embedding = bedrock.embed(_BEDROCK_CLIENT, q)
    except Exception as exc:
        # Unlike cd-etl's degrade-gracefully precedent for a stale
        # embedding, there's nothing to fall back to here -- the
        # embedding IS the query. 503 signals retryability more usefully
        # than the generic 500 catch-all below.
        logger.exception("Bedrock embed failed for query %r", q)
        raise HTTPException(
            status_code=503, detail="Search is temporarily unavailable."
        ) from exc

    vocab_match = fetch_closest_vocab_term(query_embedding)
    tier1_bills = []
    if vocab_match is not None and vocab_match["distance"] <= VOCAB_MATCH_THRESHOLD:
        fetch = (
            fetch_bills_by_policy_area
            if vocab_match["kind"] == "POLICY_AREA"
            else fetch_bills_by_subject
        )
        tier1_bills = fetch(vocab_match["term"], limit)

    remaining = limit - len(tier1_bills)
    tier2_bills = (
        fetch_bills_by_similarity(
            query_embedding, [b["bill_id"] for b in tier1_bills], remaining,
            BILL_SIMILARITY_THRESHOLD,
        )
        if remaining > 0
        else []
    )

    all_bills = tier1_bills + tier2_bills
    votes = fetch_votes_for_bills([b["bill_id"] for b in all_bills], bioguide_id)
    return shape_bill_search_response(q, bioguide_id, all_bills, votes)
