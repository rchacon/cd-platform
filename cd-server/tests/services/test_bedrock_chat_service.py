import asyncio

import pytest

from cd.server import settings
from cd.server.services.bedrock_chat_service import (
    BedrockChatClient,
    BedrockConverseError,
    get_bedrock_chat_client,
)


class _FakeBedrockClient:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


def _response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


def test_converse_returns_the_generated_text():
    fake = _FakeBedrockClient(response=_response("Voted NAY on..."))
    client = BedrockChatClient(fake, "anthropic.claude-3-5-haiku")

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


def test_converse_wraps_client_error():
    from botocore.exceptions import ClientError

    fake = _FakeBedrockClient(
        raises=ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate limited"}}, "Converse"
        )
    )
    client = BedrockChatClient(fake, "anthropic.claude-3-5-haiku")

    with pytest.raises(BedrockConverseError, match="Bedrock Converse call failed"):
        asyncio.run(client.converse("s", "u"))


def test_converse_wraps_botocore_error():
    from botocore.exceptions import NoCredentialsError

    fake = _FakeBedrockClient(raises=NoCredentialsError())
    client = BedrockChatClient(fake, "anthropic.claude-3-5-haiku")

    with pytest.raises(BedrockConverseError, match="Bedrock Converse call failed"):
        asyncio.run(client.converse("s", "u"))


@pytest.mark.parametrize(
    "malformed_response",
    [
        {},
        {"output": {}},
        {"output": {"message": {"content": []}}},
        {"output": {"message": {"content": [{}]}}},
    ],
)
def test_converse_raises_on_malformed_response_instead_of_a_raw_keyerror(malformed_response):
    fake = _FakeBedrockClient(response=malformed_response)
    client = BedrockChatClient(fake, "anthropic.claude-3-5-haiku")

    with pytest.raises(BedrockConverseError, match="Malformed Bedrock Converse response"):
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
