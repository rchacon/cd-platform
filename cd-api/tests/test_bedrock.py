import json

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


def test_build_bedrock_client_respects_aws_region_env_var(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    calls = []
    monkeypatch.setattr(
        bedrock.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bedrock.build_bedrock_client()

    assert calls[0][1]["region_name"] == "eu-west-1"
