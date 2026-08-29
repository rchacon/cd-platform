import json
import os

from cd.api import bedrock


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


def test_embed_query_returns_the_models_embedding():
    client = _FakeBedrockClient(embedding=[0.1, 0.2, 0.3])

    result = bedrock.embed_query(client, "dreamers")

    assert result == [0.1, 0.2, 0.3]


def test_embed_query_calls_titan_v2_with_expected_request_shape():
    client = _FakeBedrockClient(embedding=[0.0])

    bedrock.embed_query(client, "immigration reform")

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


def test_build_bedrock_client_sets_explicit_short_timeouts(monkeypatch):
    # Without these, botocore's 60s connect/read defaults outlast the
    # Lambda's own 25s timeout, so a broken network path to Bedrock (e.g.
    # a missing SG egress rule for 443) hangs the whole invocation into an
    # uncatchable 500 instead of an exception app.py can turn into a 503.
    # The values matter: total_max_attempts (max_attempts + 1) times a
    # single attempt's connect + read worst case must stay under the 25s
    # function budget -- 2 * (5 + 5) here, ~21s with backoff.
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock.build_bedrock_client()

    config = calls[0][1]["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 5
    assert config.retries["max_attempts"] == 1


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
    # Regression test mirroring cd-etl's own bedrock_embeddings.py fix:
    # an empty-but-present AWS_PROFILE (e.g. a container environment
    # that always defines the var, just empty when unset) makes boto3
    # raise ProfileNotFound immediately rather than falling back to the
    # default credential chain -- passing profile_name=None to
    # boto3.Session() does NOT fix this (botocore's config-provider
    # chain still reads the raw env var itself regardless); only
    # actually removing the var from the environment works.
    monkeypatch.setenv("AWS_PROFILE", "")
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: None)

    bedrock.build_bedrock_client()

    assert "AWS_PROFILE" not in os.environ


def test_build_bedrock_client_tolerates_aws_profile_already_unset(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: None)

    bedrock.build_bedrock_client()  # must not raise

    assert "AWS_PROFILE" not in os.environ
