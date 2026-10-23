import asyncio
from types import SimpleNamespace

import pytest

from cd.server import settings
from cd.server.services.bedrock_chat_service import (
    BedrockChatClient,
    BedrockConverseError,
    build_chat_model,
    get_bedrock_chat_client,
)


class _FakeBedrockClient:
    # ChatBedrockConverse reads the region off its runtime client to build
    # its own control-plane client; without it, construction would need
    # real AWS config, which CI doesn't have.
    meta = SimpleNamespace(region_name="us-west-2")

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        # A copy: ChatBedrockConverse pops "output" off the response.
        return dict(self._response)


_USAGE = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}


def _response(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": _USAGE,
    }


def _client(fake: _FakeBedrockClient) -> BedrockChatClient:
    return BedrockChatClient(build_chat_model(fake, "anthropic.claude-3-5-haiku"))


def test_converse_returns_the_generated_text():
    fake = _FakeBedrockClient(response=_response("Voted NAY on..."))
    client = _client(fake)

    result = asyncio.run(client.converse("system prompt", "user prompt"))

    assert result == "Voted NAY on..."
    assert fake.calls == [
        {
            "modelId": "anthropic.claude-3-5-haiku",
            "system": [{"text": "system prompt"}],
            "messages": [{"role": "user", "content": [{"text": "user prompt"}]}],
            "inferenceConfig": {"maxTokens": 1024, "temperature": 0.2},
        }
    ]
    assert client.model_id == "anthropic.claude-3-5-haiku"


def test_converse_wraps_client_error():
    from botocore.exceptions import ClientError

    fake = _FakeBedrockClient(
        raises=ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate limited"}}, "Converse"
        )
    )
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="Bedrock Converse call failed"):
        asyncio.run(client.converse("s", "u"))


def test_converse_wraps_botocore_error():
    from botocore.exceptions import NoCredentialsError

    fake = _FakeBedrockClient(raises=NoCredentialsError())
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="Bedrock Converse call failed"):
        asyncio.run(client.converse("s", "u"))


@pytest.mark.parametrize(
    "malformed_response",
    [
        {"usage": _USAGE},
        {"output": {}, "usage": _USAGE},
        {"output": {"message": {"content": [{}]}}, "usage": _USAGE},
    ],
)
def test_converse_raises_on_malformed_response_instead_of_a_raw_keyerror(malformed_response):
    fake = _FakeBedrockClient(response=malformed_response)
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="Malformed Bedrock Converse response"):
        asyncio.run(client.converse("s", "u"))


def test_converse_raises_when_the_generation_was_truncated_at_max_tokens():
    # stopReason=max_tokens -> the text is cut off mid-sentence; must not
    # be returned (and then persisted) as a complete summary.
    fake = _FakeBedrockClient(
        response=_response("Voted NAY on HR 1, then AYE on HR 2, then", stop_reason="max_tokens")
    )
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="truncated at maxTokens"):
        asyncio.run(client.converse("s", "u"))


@pytest.mark.parametrize("empty_text", ["", "   ", "\n\t"])
def test_converse_raises_on_an_empty_completion(empty_text):
    fake = _FakeBedrockClient(response=_response(empty_text))
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="empty completion"):
        asyncio.run(client.converse("s", "u"))


def test_converse_raises_when_the_response_has_no_content_blocks():
    fake = _FakeBedrockClient(
        response={"output": {"message": {"content": []}}, "stopReason": "end_turn", "usage": _USAGE}
    )
    client = _client(fake)

    with pytest.raises(BedrockConverseError, match="empty completion"):
        asyncio.run(client.converse("s", "u"))


def test_get_bedrock_chat_client_warns_and_constructs_when_unset_and_local(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "BEDROCK_CHAT_MODEL_ID", "")

    with caplog.at_level("WARNING"):
        client = get_bedrock_chat_client()

    assert isinstance(client, BedrockChatClient)
    assert client.model_id == ""
    assert "BEDROCK_CHAT_MODEL_ID not set" in caplog.text


def test_get_bedrock_chat_client_raises_when_unset_and_not_local(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "BEDROCK_CHAT_MODEL_ID", "")

    with pytest.raises(RuntimeError, match="BEDROCK_CHAT_MODEL_ID"):
        get_bedrock_chat_client()


def test_get_bedrock_chat_client_uses_the_configured_model_id(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_CHAT_MODEL_ID", "anthropic.claude-3-5-haiku")

    client = get_bedrock_chat_client()

    assert client.model_id == "anthropic.claude-3-5-haiku"
