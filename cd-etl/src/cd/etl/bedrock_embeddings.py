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
    # No explicit region_name -- same precedent as cd-server's own
    # boto3.client("lambda") (services/cd_api_service.py), relying on
    # boto3's own default region resolution (the ECS task's own
    # configured region) rather than hardcoding a fallback that could
    # silently be wrong if that ever changes.
    return boto3.client("bedrock-runtime")


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
