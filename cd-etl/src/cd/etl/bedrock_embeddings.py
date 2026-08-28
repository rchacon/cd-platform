"""Embedding generation via AWS Bedrock, for cd-platform#9 (semantic
search over bill subjects).

IAM/task-role auth via boto3's own default credential chain -- no API
key/secret to manage, unlike congress_api.py's CONGRESS_API_KEY. Model
choice (Titan Text Embeddings V2) was picked over OpenAI/Cohere
specifically for this: cost is a wash across all three at this
project's scale, and Titan avoids a new external secret entirely,
matching every other AWS-native auth pattern already in this project.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"

# Titan V2 supports 256/512/1024-dim output. 1024 is chosen for
# retrieval quality: Titan pricing is per-token, not per-dimension, so
# there's no cost reason to go smaller, and this project's scale (a few
# thousand bills) makes the storage/index cost difference irrelevant
# either way. Must match migration 0005's vector(1024) columns.
EMBEDDING_DIMENSIONS = 1024


def build_bedrock_client() -> Any:
    # Unlike cd-server's boto3.client("lambda") (services/cd_api_service.py,
    # no region_name at all), this needs an explicit fallback: that
    # client is only ever actually constructed in a non-"local"
    # CD_SERVER_ENVIRONMENT (get_cd_api_service() picks HttpApiClient
    # instead when local), so it never runs anywhere a region can't be
    # resolved. bills_etl.py/house_votes_etl.py build this
    # unconditionally at module import time -- confirmed empirically
    # that boto3.client() itself (unlike credential resolution, which is
    # lazy) requires a resolvable region just to construct, raising
    # botocore.exceptions.NoRegionError immediately in any environment
    # (e.g. local dev, dag-processor parsing) with no AWS config at all.
    # Matters beyond local dev too: an import-time failure here would be
    # the exact same class of bug as rchacon/cd-platform#79 (CONGRESS_API_KEY
    # read at import time broke dag-processor, which parses DAG files but
    # is never given that credential) -- dag-processor doesn't need a
    # real Bedrock call to succeed, just to be able to import this module
    # at all.
    #
    # An empty-but-present AWS_PROFILE must be treated as unset, for the
    # same import-time-crash reason: docker-compose.yml's
    # AWS_PROFILE: ${AWS_PROFILE:-} always defines the env var inside
    # the container, just empty when unset in .env. Confirmed
    # empirically that passing profile_name=None to boto3.Session()
    # does NOT fix this on its own -- botocore's config-provider chain
    # still reads the raw AWS_PROFILE env var itself regardless of what
    # profile_name is given, so an empty string there still raises
    # ProfileNotFound immediately (this crashed CI, which has no .env
    # at all). Actually removing the var from the environment when
    # empty is the only fix that works.
    if not os.environ.get("AWS_PROFILE"):
        os.environ.pop("AWS_PROFILE", None)
    return boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def embed_text(client: Any, text: str) -> list[float]:
    response = client.invoke_model(
        modelId=TITAN_EMBED_MODEL_ID,
        body=json.dumps({
            "inputText": text,
            "dimensions": EMBEDDING_DIMENSIONS,
            "normalize": True,
        }),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]
