import json
import os

from cd.etl import bedrock_embeddings


class _FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _FakeBedrockClient:
    def __init__(self, embedding):
        self._embedding = embedding
        self.calls = []

    def invoke_model(self, modelId, body, contentType, accept):
        self.calls.append({
            "modelId": modelId, "body": json.loads(body),
            "contentType": contentType, "accept": accept,
        })
        return {"body": _FakeBody({"embedding": self._embedding})}


def test_embed_text_returns_the_models_embedding():
    client = _FakeBedrockClient(embedding=[0.1, 0.2, 0.3])

    result = bedrock_embeddings.embed_text(client, "dreamers")

    assert result == [0.1, 0.2, 0.3]


def test_embed_text_calls_titan_v2_with_expected_request_shape():
    client = _FakeBedrockClient(embedding=[0.0])

    bedrock_embeddings.embed_text(client, "immigration reform")

    call = client.calls[0]
    assert call["modelId"] == "amazon.titan-embed-text-v2:0"
    assert call["body"] == {
        "inputText": "immigration reform",
        "dimensions": bedrock_embeddings.EMBEDDING_DIMENSIONS,
        "normalize": True,
    }
    assert call["contentType"] == "application/json"
    assert call["accept"] == "application/json"


def test_build_bedrock_client_targets_bedrock_runtime(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bedrock_embeddings.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock_embeddings.build_bedrock_client()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("bedrock-runtime",)
    assert "region_name" in kwargs  # falls back to a default -- must never raise NoRegionError


def test_build_bedrock_client_respects_aws_region_env_var(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    calls = []
    monkeypatch.setattr(
        bedrock_embeddings.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock_embeddings.build_bedrock_client()

    assert calls[0][1]["region_name"] == "eu-west-1"


def test_build_bedrock_client_leaves_a_real_aws_profile_env_var_untouched(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "local-bedrock")
    monkeypatch.setattr(bedrock_embeddings.boto3, "client", lambda *args, **kwargs: None)

    bedrock_embeddings.build_bedrock_client()

    assert os.environ["AWS_PROFILE"] == "local-bedrock"


def test_build_bedrock_client_clears_an_empty_aws_profile_env_var(monkeypatch):
    # Regression test: docker-compose.yml's AWS_PROFILE: ${AWS_PROFILE:-}
    # always defines this env var inside the container, just empty when
    # unset in .env -- boto3 treats an empty-but-present AWS_PROFILE as
    # "load a profile literally named ''", raising ProfileNotFound
    # immediately (confirmed empirically: this crashed CI, which has no
    # .env at all -- passing profile_name=None to boto3.Session() does
    # NOT fix this, since botocore's config-provider chain still reads
    # the raw env var itself regardless). Only actually removing the
    # var from the environment works.
    monkeypatch.setenv("AWS_PROFILE", "")
    monkeypatch.setattr(bedrock_embeddings.boto3, "client", lambda *args, **kwargs: None)

    bedrock_embeddings.build_bedrock_client()

    assert "AWS_PROFILE" not in os.environ


def test_build_bedrock_client_tolerates_aws_profile_already_unset(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(bedrock_embeddings.boto3, "client", lambda *args, **kwargs: None)

    bedrock_embeddings.build_bedrock_client()  # must not raise

    assert "AWS_PROFILE" not in os.environ
