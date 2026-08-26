"""Embedding generation via AWS Bedrock, for cd-platform#9's GET
/bills/search -- embeds the caller's free-text query at request time.

Not shared with cd-etl's near-identical bedrock_embeddings.py via
cd-lib: cd-etl doesn't depend on cd-lib yet, and adopting it there for
the first time (namespace-package cd/__init__.py removal, Docker
build-context-at-repo-root) is a bigger structural change than this
feature needs to carry. Revisit only if a third consumer needs it too.

IAM auth via boto3's own default credential chain (the Lambda
execution role's task credentials) -- no API key/secret to manage.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"

# Must match migration 0005's vector(1024) columns (cd-etl) -- a query
# embedding is compared directly against those stored vectors, so the
# dimensions must match exactly.
EMBEDDING_DIMENSIONS = 1024


def build_bedrock_client() -> Any:
    # An explicit region_name fallback, not boto3's own default
    # resolution: this is constructed unconditionally at module import
    # time (app.py, Lambda cold start), and boto3.client() itself
    # (unlike credential resolution, which is lazy) requires a
    # resolvable region just to construct, raising
    # botocore.exceptions.NoRegionError immediately in any environment
    # with no AWS config at all (e.g. local dev, or a test importing
    # app.py without AWS_REGION set) -- same class of import-time-crash
    # bug as cd-etl's own build_bedrock_client(), which cites
    # rchacon/cd-platform#79 for the same reasoning.
    return boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def embed_query(client: Any, text: str) -> list[float]:
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
