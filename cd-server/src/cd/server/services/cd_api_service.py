import asyncio
import json
from abc import ABC, abstractmethod

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from cd.lib.models import Member, MembersResponse

from cd.server import settings


class ApiClientError(Exception):
    """Raised by every ApiClient implementation on a non-2xx response from
    cd-api, so callers get one consistent error type regardless of which
    implementation get_cd_api_service() picked."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"cd-api returned {status_code}: {body}")


class ApiClient(ABC):
    """Transport interface HttpApiClient/LambdaApiClient both implement,
    so the two can't silently drift apart (a subclass missing get()
    raises TypeError at instantiation, not just at first call). Internal
    detail of this module -- callers outside it should go through
    CdApiService below, not an ApiClient directly."""

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
        try:
            response = await self._client.get(f"{self.base_url}{path}", params=query)
        except httpx.HTTPError as e:
            # Connection refused, timeout, DNS failure, etc. -- there's no
            # HTTP response here to read a status code from at all, unlike
            # the is_error branch below. 502 (bad gateway) matches cd-server
            # acting as a gateway to an unreachable upstream.
            raise ApiClientError(502, str(e)) from e
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
        try:
            response = await asyncio.to_thread(
                self.lambda_client.invoke,
                FunctionName=self.function_name,
                Payload=json.dumps(event),
            )
        except (ClientError, BotoCoreError) as e:
            # ClientError/BotoCoreError don't share a common base besides
            # Exception -- ClientError covers AWS API-level failures (bad
            # function name, missing lambda:InvokeFunction permission),
            # BotoCoreError covers lower-level ones (no credentials,
            # can't reach the Lambda API endpoint at all). Neither
            # produces a {statusCode, body} response to inspect below.
            raise ApiClientError(502, str(e)) from e

        # A crash inside the Lambda runtime itself (as opposed to a normal
        # non-2xx HTTP response cd-api's own error handlers produced) sets
        # this instead of returning the usual {statusCode, body} shape --
        # Payload here is the raw Lambda error JSON, not RFC 9457.
        if response.get("FunctionError"):
            raise ApiClientError(500, response["Payload"].read().decode())

        try:
            result = json.load(response["Payload"])
            status_code = result["statusCode"]
            body = result["body"]
        except (KeyError, json.JSONDecodeError) as e:
            # Shouldn't happen given Mangum's own contract, but a
            # malformed/unexpected payload here shouldn't surface as a
            # raw KeyError either -- same "one consistent error type"
            # promise as every other failure path in this class.
            raise ApiClientError(502, f"Malformed Lambda response: {e}") from e

        if status_code >= 400:
            raise ApiClientError(status_code, body)

        return json.loads(body)


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


class CdApiService:
    """The service layer schema.py depends on for cd-api data. Unlike the
    ApiClient it wraps (which just makes the call and hands back a raw
    dict), this validates cd-api's response against the shared
    Member/MembersResponse models from cd-lib and returns real Member
    objects -- the response-shape trust boundary lives here, not in
    schema.py's resolvers."""

    def __init__(self, transport: ApiClient):
        self._transport = transport

    async def get_representatives(self, state: str, district: int) -> list[Member]:
        result = await self._transport.get(
            "/members", {"state": state, "district": str(district)}
        )
        return MembersResponse(**result).representatives

    async def get_senators(self, state: str) -> list[Member]:
        result = await self._transport.get("/members", {"state": state})
        return MembersResponse(**result).senators

    async def aclose(self) -> None:
        await self._transport.aclose()


def get_cd_api_service() -> CdApiService:
    if settings.ENVIRONMENT == "local":
        return CdApiService(HttpApiClient(settings.CD_API_BASE_URL))
    if not settings.CD_API_FUNCTION_NAME:
        # Called at import time (schema.py's module-level cd_api_service =
        # get_cd_api_service()), so a misconfigured non-local deploy fails
        # immediately at startup instead of lazily on the first real
        # GraphQL query with a boto3 ParamValidationError.
        raise RuntimeError(
            'CD_API_FUNCTION_NAME must be set when CD_SERVER_ENVIRONMENT is not "local".'
        )
    return CdApiService(LambdaApiClient(settings.CD_API_FUNCTION_NAME))
