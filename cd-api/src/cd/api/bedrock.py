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
from botocore.config import Config

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
    #
    # An empty-but-present AWS_PROFILE must be treated as unset, for the
    # same reasoning as cd-etl's own fix (e.g. a container environment
    # that always defines the var, just empty when unset in .env).
    # Confirmed empirically there that passing profile_name=None to
    # boto3.Session() does NOT fix this -- botocore's config-provider
    # chain still reads the raw AWS_PROFILE env var itself regardless of
    # what profile_name is given. Actually removing the var from the
    # environment when empty is the only fix that works.
    if not os.environ.get("AWS_PROFILE"):
        os.environ.pop("AWS_PROFILE", None)

    # botocore's own defaults are a 60s connect timeout and a 60s read
    # timeout, both longer than the cd-platform-cd-api Lambda's own 25s
    # function timeout. So when the network path to Bedrock is broken --
    # e.g. the Lambda's security group is missing an egress rule for 443,
    # which is exactly how GET /bills/search first shipped -- invoke_model()
    # hangs past the Lambda timeout and the whole sandbox is killed
    # mid-call. That surfaces to the caller as an uncatchable 500 (API
    # Gateway's own), never reaching app.py's try/except around
    # embed_query() that would otherwise turn a Bedrock failure into a
    # clean, retryable 503.
    #
    # The numbers are chosen so the *whole* call -- every retry, both
    # phases -- stays inside the 25s function budget with room left for
    # the handler's own DB round trips, keeping that failure mode
    # catchable rather than just moving the cliff. total_max_attempts is
    # max_attempts + 1 == 2, and a single attempt's worst case is
    # connect_timeout + read_timeout == 10s, so the absolute ceiling is
    # ~21s (2 * 10s + one standard-mode backoff). A healthy embed of a
    # <=500-char query returns in well under 1s, so 5s read is already
    # generous; 5s connect is the conventional AWS-SDK floor and tolerates
    # a cold VPC Lambda's first TLS-through-NAT handshake. One retry, not
    # more, is what keeps the ceiling under budget -- more real headroom
    # needs the Lambda's own timeout/memory raised (cd-infra#58).
    #
    # Built fresh per call, not a module-level constant -- botocore
    # mutates config.retries in place when the client is created (it
    # replaces max_attempts with total_max_attempts), so a shared Config
    # instance is a footgun.
    config = Config(
        connect_timeout=5,
        read_timeout=5,
        retries={"max_attempts": 1, "mode": "standard"},
    )
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        config=config,
    )


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
