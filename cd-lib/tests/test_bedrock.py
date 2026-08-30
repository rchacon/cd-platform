import json
import os

from botocore.config import Config

from cd.lib import bedrock


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


def test_embed_returns_the_models_embedding():
    client = _FakeBedrockClient(embedding=[0.1, 0.2, 0.3])

    result = bedrock.embed(client, "dreamers")

    assert result == [0.1, 0.2, 0.3]


def test_embed_calls_titan_v2_with_expected_request_shape():
    client = _FakeBedrockClient(embedding=[0.0])

    bedrock.embed(client, "immigration reform")

    call = client.calls[0]
    assert call["modelId"] == "amazon.titan-embed-text-v2:0"
    assert call["body"] == {
        "inputText": "immigration reform",
        "dimensions": bedrock.EMBEDDING_DIMENSIONS,
        "normalize": True,
    }
    assert call["contentType"] == "application/json"
    assert call["accept"] == "application/json"


def test_build_bedrock_client_targets_bedrock_runtime(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock.build_bedrock_client()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("bedrock-runtime",)
    assert "region_name" in kwargs  # falls back to a default -- must never raise NoRegionError


def test_build_bedrock_client_omits_config_when_none_given(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock.build_bedrock_client()

    assert "config" not in calls[0][1]


def test_build_bedrock_client_passes_the_given_config_through(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    config = Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 1})

    bedrock.build_bedrock_client(config)

    assert calls[0][1]["config"] is config


def test_build_bedrock_client_respects_aws_region_env_var(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock.build_bedrock_client()

    assert calls[0][1]["region_name"] == "eu-west-1"


def test_build_bedrock_client_leaves_a_real_aws_profile_env_var_untouched(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "local-bedrock")
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: None)

    bedrock.build_bedrock_client()

    assert os.environ["AWS_PROFILE"] == "local-bedrock"


def test_build_bedrock_client_clears_an_empty_aws_profile_env_var(monkeypatch):
    # An empty-but-present AWS_PROFILE (a container that always defines
    # `AWS_PROFILE: ${AWS_PROFILE:-}`, just empty when unset) makes
    # botocore raise ProfileNotFound at construction rather than falling
    # back to the default credential chain. Passing profile_name=None does
    # NOT fix this (botocore's config-provider chain reads the raw env var
    # regardless) -- only removing the var from the environment works.
    monkeypatch.setenv("AWS_PROFILE", "")
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: None)

    bedrock.build_bedrock_client()

    assert "AWS_PROFILE" not in os.environ


def test_build_bedrock_client_tolerates_aws_profile_already_unset(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: None)

    bedrock.build_bedrock_client()  # must not raise

    assert "AWS_PROFILE" not in os.environ
