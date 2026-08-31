"""Embedding generation via AWS Bedrock (Amazon Titan Text Embeddings V2).

Shared by cd-api (`GET /bills` -- embeds a query at request time)
and cd-etl (`bills_common.sync_bill` -- embeds a bill's title + CRS
summary). Titan V2 was picked over OpenAI/Cohere specifically to avoid a
new external secret: cost is a wash at this project's scale, and IAM /
task-role auth via boto3's default credential chain matches every other
AWS-native pattern here -- no API key to manage.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.config import Config

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"

# Titan V2 supports 256/512/1024-dim output; 1024 is chosen for
# retrieval quality (Titan pricing is per-token, not per-dimension, so
# there's no cost reason to go smaller). Must match cd-etl migration
# 0005's vector(1024) columns -- a query embedding is compared directly
# against those stored vectors, so the dimensions must match exactly.
EMBEDDING_DIMENSIONS = 1024


def build_bedrock_client(config: Config | None = None) -> Any:
    """A `bedrock-runtime` client.

    Region comes from `AWS_REGION`, falling back to `us-west-2`. That
    explicit fallback (not boto3's own resolution) matters because
    `boto3.client()` itself -- unlike credential resolution, which is
    lazy -- needs a resolvable region just to construct, raising
    `botocore.exceptions.NoRegionError` immediately. Both callers build
    a client at module import time (cd-api's Lambda cold start; cd-etl's
    DAG modules), so an unresolvable region there would be an
    import-time crash in any environment with no AWS config at all --
    local dev, a test importing the module, Airflow's dag-processor
    parsing DAGs. Same class of bug as rchacon/cd-platform#79
    (CONGRESS_API_KEY read at import time broke dag-processor).

    An empty-but-present `AWS_PROFILE` is treated as unset: a container
    that always defines `AWS_PROFILE: ${AWS_PROFILE:-}` leaves it as `""`
    when nothing is set in `.env`, and botocore then raises
    `ProfileNotFound` at construction. Passing `profile_name=None` does
    NOT fix this -- botocore's config-provider chain reads the raw env
    var regardless -- only removing the var from the environment does.

    Pass a `Config` to bound the call's worst case (cd-api does: its
    Lambda's 25s function timeout is well under botocore's 60s connect
    + 60s read defaults, so a broken network path to Bedrock would
    otherwise hang past the timeout into an uncatchable 500). Omit it
    for boto3's defaults. Build the `Config` fresh per client, never a
    module constant: botocore mutates `config.retries` in place at
    construction (`max_attempts` -> `total_max_attempts`), so a reused
    instance is a footgun.
    """
    if not os.environ.get("AWS_PROFILE"):
        os.environ.pop("AWS_PROFILE", None)

    kwargs: dict[str, Any] = {"region_name": os.environ.get("AWS_REGION", "us-west-2")}
    if config is not None:
        kwargs["config"] = config
    return boto3.client("bedrock-runtime", **kwargs)


def embed(client: Any, text: str) -> list[float]:
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
