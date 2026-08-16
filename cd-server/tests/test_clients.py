import asyncio
import io
import json

import httpx
import pytest

from cd.server import settings
from cd.server.clients import (
    ApiClient,
    ApiClientError,
    HttpApiClient,
    LambdaApiClient,
    _build_gateway_event,
    get_api_client,
)


def test_both_clients_implement_the_shared_interface():
    assert issubclass(HttpApiClient, ApiClient)
    assert issubclass(LambdaApiClient, ApiClient)


def test_incomplete_subclass_cannot_be_instantiated():
    class Incomplete(ApiClient):
        pass

    with pytest.raises(TypeError):
        Incomplete()


def test_build_gateway_event_shape():
    event = _build_gateway_event("/v1/members", {"state": "CA", "district": "12"})
    assert event["resource"] == "/{proxy+}"
    assert event["path"] == "/v1/members"
    assert event["httpMethod"] == "GET"
    assert event["multiValueQueryStringParameters"] == {"state": ["CA"], "district": ["12"]}
    assert "requestContext" in event


def test_build_gateway_event_empty_query():
    event = _build_gateway_event("/v1/version", {})
    assert event["multiValueQueryStringParameters"] == {}


def test_http_api_client_returns_json_on_success(monkeypatch):
    async def fake_get(self, url, params=None):
        assert url == "http://cd-api:8000/version"
        assert params == {}
        return httpx.Response(200, json={"version": "1.2.3"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = HttpApiClient("http://cd-api:8000")
    assert asyncio.run(client.get("/version", {})) == {"version": "1.2.3"}


def test_http_api_client_passes_query_params(monkeypatch):
    captured = {}

    async def fake_get(self, url, params=None):
        captured["url"] = url
        captured["params"] = params
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    asyncio.run(HttpApiClient("http://cd-api:8000").get("/members", {"state": "CA"}))
    assert captured["url"] == "http://cd-api:8000/members"
    assert captured["params"] == {"state": "CA"}


def test_http_api_client_raises_on_error_response(monkeypatch):
    async def fake_get(self, url, params=None):
        return httpx.Response(
            404, json={"detail": "not found"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(HttpApiClient("http://cd-api:8000").get("/members", {"state": "ZZ"}))
    assert exc_info.value.status_code == 404


def test_http_api_client_aclose_closes_underlying_client():
    client = HttpApiClient("http://cd-api:8000")
    assert client._client.is_closed is False
    asyncio.run(client.aclose())
    assert client._client.is_closed is True


class _FakeLambdaClient:
    def __init__(self, payload: dict, function_error: str | None = None):
        self._payload = payload
        self._function_error = function_error
        self.last_invoke_kwargs: dict | None = None

    def invoke(self, FunctionName, Payload):
        self.last_invoke_kwargs = {"FunctionName": FunctionName, "Payload": Payload}
        response = {"Payload": io.BytesIO(json.dumps(self._payload).encode())}
        if self._function_error:
            response["FunctionError"] = self._function_error
        return response


def test_lambda_api_client_returns_json_on_success(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 200, "body": json.dumps({"version": "1.2.3"})})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    client = LambdaApiClient("cd-platform-cd-api")
    assert asyncio.run(client.get("/version", {})) == {"version": "1.2.3"}


def test_lambda_api_client_builds_v1_prefixed_event_with_query(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 200, "body": "{}"})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/members", {"state": "CA"}))

    assert fake.last_invoke_kwargs["FunctionName"] == "cd-platform-cd-api"
    event = json.loads(fake.last_invoke_kwargs["Payload"])
    assert event["path"] == "/v1/members"
    assert event["multiValueQueryStringParameters"] == {"state": ["CA"]}


def test_lambda_api_client_raises_on_error_status(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 404, "body": json.dumps({"detail": "not found"})})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/members", {"state": "ZZ"}))
    assert exc_info.value.status_code == 404


def test_lambda_api_client_raises_on_function_error(monkeypatch):
    fake = _FakeLambdaClient({"errorMessage": "boom"}, function_error="Unhandled")
    monkeypatch.setattr("boto3.client", lambda service: fake)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/version", {}))
    assert exc_info.value.status_code == 500


def test_lambda_api_client_aclose_is_a_noop(monkeypatch):
    monkeypatch.setattr("boto3.client", lambda service: _FakeLambdaClient({}))
    client = LambdaApiClient("cd-platform-cd-api")
    asyncio.run(client.aclose())  # should not raise


def test_get_api_client_returns_http_client_for_local(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "CD_API_BASE_URL", "http://example:8000")
    client = get_api_client()
    assert isinstance(client, HttpApiClient)
    assert client.base_url == "http://example:8000"


def test_get_api_client_returns_lambda_client_otherwise(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "CD_API_FUNCTION_NAME", "cd-platform-cd-api")
    monkeypatch.setattr("boto3.client", lambda service: _FakeLambdaClient({}))
    client = get_api_client()
    assert isinstance(client, LambdaApiClient)
    assert client.function_name == "cd-platform-cd-api"
