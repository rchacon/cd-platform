import asyncio
import json
from abc import ABC, abstractmethod

import boto3
import httpx

from cd.server import settings


class ApiClientError(Exception):
    """Raised by every ApiClient implementation on a non-2xx response from
    cd-api, so callers get one consistent error type regardless of which
    implementation get_api_client() picked."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"cd-api returned {status_code}: {body}")


class ApiClient(ABC):
    """Common interface HttpApiClient/LambdaApiClient both implement, so
    the two can't silently drift apart (a subclass missing get() raises
    TypeError at instantiation, not just at first call)."""

    @abstractmethod
    async def get(self, path: str, query: dict[str, str]) -> dict:
        ...

    async def aclose(self) -> None:
        """Release any held resources (e.g. an open connection pool).
        Default no-op -- only HttpApiClient currently needs this."""


class HttpApiClient(ApiClient):
    def __init__(self, base_url: str):
        self.base_url = base_url
        # One client, reused across every get() call rather than opening
        # a fresh connection per request -- httpx's own docs recommend
        # against a new client per call for exactly this reason
        # (connection-pool/TCP-handshake overhead). Closed via aclose(),
        # called from app.py's lifespan on shutdown.
        self._client = httpx.AsyncClient()

    async def get(self, path: str, query: dict[str, str]) -> dict:
        response = await self._client.get(f"{self.base_url}{path}", params=query)
        if response.is_error:
            raise ApiClientError(response.status_code, response.text)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class LambdaApiClient(ApiClient):
    """Calls cd-api's real Lambda function directly via boto3, bypassing
    API Gateway entirely -- no network hop, and no X-Api-Key needed
    (cd-api's own code never checks that header; only API Gateway does,
    see cd-api/src/cd/api/app.py's own DESCRIPTION). Dogfoods cd-api's
    actual FastAPI app as much as possible by building a synthetic
    API-Gateway-shaped event and invoking the same Mangum handler entry
    point real requests go through, rather than reaching around it to
    call cd-api's internal Python functions directly -- routing,
    validation, and RFC 9457 error formatting all get exercised exactly
    as they would over real HTTP.
    """

    def __init__(self, function_name: str):
        self.lambda_client = boto3.client("lambda")
        self.function_name = function_name

    async def get(self, path: str, query: dict[str, str]) -> dict:
        # cd-api's Mangum handler strips a leading /v1 (api_gateway_base_path)
        # before routing -- included here so this synthetic event matches
        # what real API Gateway forwards in production (cd-infra#19),
        # exercising the same stripping logic this shortcut would
        # otherwise never touch.
        event = _build_gateway_event(f"/v1{path}", query)

        # boto3 has no async API at all -- invoke() is a genuinely
        # blocking network call. asyncio.to_thread() runs it in a thread
        # pool so it doesn't block the event loop, without needing a
        # third-party async-boto3 wrapper (aioboto3/aiobotocore) for what
        # this class does with just this one call.
        response = await asyncio.to_thread(
            self.lambda_client.invoke,
            FunctionName=self.function_name,
            Payload=json.dumps(event),
        )

        # A crash inside the Lambda runtime itself (as opposed to a normal
        # non-2xx HTTP response cd-api's own error handlers produced) sets
        # this instead of returning the usual {statusCode, body} shape --
        # Payload here is the raw Lambda error JSON, not RFC 9457.
        if response.get("FunctionError"):
            raise ApiClientError(500, response["Payload"].read().decode())

        result = json.load(response["Payload"])
        if result["statusCode"] >= 400:
            raise ApiClientError(result["statusCode"], result["body"])

        return json.loads(result["body"])


def _build_gateway_event(path: str, query: dict[str, str]) -> dict:
    # Minimal shape mirrors cd-api/tests/test_app.py's own
    # _api_gateway_event helper, verified against Mangum's actual event
    # parsing (mangum.handlers.api_gateway.APIGateway) rather than
    # guessed: `resource`+`requestContext` are what Mangum's own type
    # detection (APIGateway.infer) checks for, `httpMethod`/`path` are
    # read directly (KeyError if absent), and query params come from
    # multiValueQueryStringParameters.
    return {
        "resource": "/{proxy+}",
        "path": path,
        "httpMethod": "GET",
        "headers": {},
        "multiValueQueryStringParameters": {key: [value] for key, value in query.items()},
        "requestContext": {"identity": {"sourceIp": "127.0.0.1"}},
        "body": None,
        "isBase64Encoded": False,
    }


def get_api_client() -> ApiClient:
    if settings.ENVIRONMENT == "local":
        return HttpApiClient(settings.CD_API_BASE_URL)
    return LambdaApiClient(settings.CD_API_FUNCTION_NAME)
