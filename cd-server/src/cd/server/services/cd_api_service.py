import asyncio
import json
from abc import ABC, abstractmethod

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from cd.lib.jsonapi import CollectionDocument, Document
from cd.lib.models import Bill, Member, MemberDetail, RollCallVote

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
    def __init__(self, base_url: str, origin_secret: str = ""):
        self.base_url = base_url
        # Locally this calls back into cd-server's own /v1, which
        # OriginGuard protects. A configured CD_API_ORIGIN_SECRET is
        # enforced even in local dev, so send it the way API Gateway would
        # -- otherwise setting it locally 403s every member/bill resolver.
        headers = {"X-Origin-Verify": origin_secret} if origin_secret else {}
        # One client, reused across every get() call rather than opening
        # a fresh connection per request -- httpx's own docs recommend
        # against a new client per call for exactly this reason
        # (connection-pool/TCP-handshake overhead). Closed via aclose(),
        # called from app.py's lifespan on shutdown.
        self._client = httpx.AsyncClient(headers=headers)

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


class AsgiApiClient(ApiClient):
    """Calls cd.api in this process through httpx's ASGI transport -- no
    network hop and no running server. Used by the agent evals and the
    MCP server, which run cd.api standalone, so the caller opens and
    closes cd.api.db's pool itself (no lifespan runs here). Imports cd.api
    lazily so the HTTP and Lambda paths never load it."""

    def __init__(self):
        from cd.api.app import app as api_app

        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://cd-api"
        )

    async def get(self, path: str, query: dict[str, str]) -> dict:
        response = await self._client.get(path, params=query)
        if response.is_error:
            raise ApiClientError(response.status_code, response.text)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class LambdaApiClient(ApiClient):
    """Calls cd-api's real Lambda function directly via boto3, bypassing
    API Gateway entirely -- no network hop, and no X-Api-Key needed
    (cd-api's own code never checks that header; only API Gateway does,
    see cd/api/app.py's own DESCRIPTION). Dogfoods cd-api's
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
    # Minimal shape mirrors tests/api/test_app.py's own
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


def _members(payload: dict, *, chamber: str, district: int | None = None) -> list[Member]:
    """Flatten cd-api's JSON:API `/members` collection document into a
    `list[Member]` for one chamber ("senators" | "representatives"),
    optionally narrowed to one district.

    cd-api's `/members` returns `CollectionDocument[MemberDetail]` (since
    cd-api-v0.3.8, cd-platform#104 PR B). It's validated through that
    model here -- a malformed envelope raises a `ValidationError`, not a
    bare `KeyError` -- then each resource's `id` becomes `bioguide_id`
    and its `state`/`in_office` attributes are dropped (not `Member`
    fields).

    The `chamber`/`district` filtering is a client-side backstop:
    `get_senators`/`get_representatives` already send `filter[chamber]`
    (and `filter[district]`) so cd-api narrows server-side, but a
    broken/ignored server-side filter must not silently return the whole
    state's delegation.
    """
    document = CollectionDocument[MemberDetail].model_validate(payload)
    members = [
        Member(
            bioguide_id=resource.id,
            **resource.attributes.model_dump(exclude={"state", "in_office"}),
        )
        for resource in document.data
    ]

    # district is NULL only for a Senator (0/1+ for every House seat,
    # Delegates and the Resident Commissioner included).
    if chamber == "senators":
        members = [m for m in members if m.district is None]
    else:
        members = [m for m in members if m.district is not None]
        if district is not None:
            members = [m for m in members if m.district == district]
    return members


class CdApiService:
    """The service layer schema.py depends on for cd-api data. Unlike the
    ApiClient it wraps (which just makes the call and hands back a raw
    dict), this validates cd-api's response against cd-lib's
    `CollectionDocument[MemberDetail]` and returns real `Member` objects
    -- the response-shape trust boundary lives here, not in schema.py's
    resolvers."""

    def __init__(self, transport: ApiClient):
        self._transport = transport

    async def get_representatives(self, state: str, district: int) -> list[Member]:
        # One filtered call -- cd-api's honest `/members` collection
        # (cd-platform#104 PR B) narrows to this state's House seat for
        # the district server-side; `_members` re-applies the same filter
        # as a backstop.
        result = await self._transport.get(
            "/members",
            {
                "filter[state]": state,
                "filter[chamber]": "house",
                "filter[district]": str(district),
            },
        )
        return _members(result, chamber="representatives", district=district)

    async def get_senators(self, state: str) -> list[Member]:
        result = await self._transport.get(
            "/members",
            {"filter[state]": state, "filter[chamber]": "senate"},
        )
        return _members(result, chamber="senators")

    async def find_members(
        self,
        name: str | None = None,
        state: str | None = None,
        chamber: str | None = None,
        district: int | None = None,
    ) -> CollectionDocument[MemberDetail]:
        """cd-api `GET /members` with whichever filters are given -- the
        research agent's member lookup (cd-platform#164), by name across
        every state or by state/chamber/district. Returns the validated
        collection whole, so each resource keeps its `state`, which the
        flattened `Member` drops. cd-api needs `name` or `state` (`422`
        otherwise), and `district` needs `state`; both surface as
        `ApiClientError`.
        """
        params = {
            key: value
            for key, value in (
                ("filter[name]", name),
                ("filter[state]", state),
                ("filter[chamber]", chamber),
                ("filter[district]", None if district is None else str(district)),
            )
            if value is not None
        }
        result = await self._transport.get("/members", params)
        return CollectionDocument[MemberDetail].model_validate(result)

    async def member_detail(self, bioguide_id: str) -> Document[MemberDetail]:
        """cd-api `GET /members/{bioguide_id}` -- one current-Congress
        member (sitting *or* departed mid-term, `in_office` flag),
        for cd-webapp's deep-linkable member detail page.

        Returns the validated JSON:API single-resource document whole:
        the bioguide id is `data.id` (not an attribute), and
        `data.attributes` is the full `MemberDetail` set -- the list's
        `member` fields plus `state` and `in_office`, which the list drops
        but a detail page needs.

        A malformed envelope raises `pydantic.ValidationError`; a `404`
        (an id with no current-Congress term -- e.g. a past-Congress
        member) propagates as `ApiClientError` from the transport.
        """
        result = await self._transport.get(f"/members/{bioguide_id}", {})
        return Document[MemberDetail].model_validate(result)

    async def search_bills(
        self, query: str, page_size: int | None = None
    ) -> CollectionDocument[Bill]:
        """cd-api `GET /bills` -- semantic search over synced bills
        (cd-platform#9). Returns the validated JSON:API collection whole,
        not a flat list: `BillSearchService` needs each resource's `id`
        (the canonical bill key, to zip with `/votes`) and per-resource
        `meta.matches` (why the bill surfaced), plus the document
        `meta.query`.

        A malformed envelope raises `pydantic.ValidationError` (the
        response-shape trust boundary, same as `_members`); a non-2xx
        (e.g. `503` when Bedrock is down) propagates as `ApiClientError`
        from the transport.
        """
        params = {"filter[query]": query}
        if page_size is not None:
            params["page[size]"] = str(page_size)
        result = await self._transport.get("/bills", params)
        return CollectionDocument[Bill].model_validate(result)

    async def member_votes(
        self, bioguide_id: str, bill_keys: list[str]
    ) -> CollectionDocument[RollCallVote]:
        """cd-api `GET /members/{bioguide_id}/votes?filter[bill]=...` --
        how one member voted on a specific set of bills. Returns the
        validated JSON:API collection whole: `BillSearchService` groups
        the `roll_call_vote` resources onto search results by
        `relationships.bill.data.id`, and reads the document
        `meta.bills_without_votes` for the "matched, no vote on record"
        state.

        `bill_keys` must be non-empty (cd-api `422`s an empty
        `filter[bill]`) and hold at most 50 ids. `404` for an unknown
        member and `400` for a malformed id both propagate as
        `ApiClientError`.
        """
        result = await self._transport.get(
            f"/members/{bioguide_id}/votes",
            {"filter[bill]": ",".join(bill_keys)},
        )
        return CollectionDocument[RollCallVote].model_validate(result)

    async def aclose(self) -> None:
        await self._transport.aclose()


def get_cd_api_service() -> CdApiService:
    if settings.ENVIRONMENT == "local":
        return CdApiService(
            HttpApiClient(settings.CD_API_BASE_URL, settings.CD_API_ORIGIN_SECRET)
        )
    if not settings.CD_API_FUNCTION_NAME:
        # Called at import time (schema.py's module-level cd_api_service =
        # get_cd_api_service()), so a misconfigured non-local deploy fails
        # immediately at startup instead of lazily on the first real
        # GraphQL query with a boto3 ParamValidationError.
        raise RuntimeError(
            'CD_API_FUNCTION_NAME must be set when CD_SERVER_ENVIRONMENT is not "local".'
        )
    # The Lambda runs the last cd-api-v* build, frozen since its source
    # moved into cd.api: changes to cd/api/ ship to /v1 but not to these
    # resolvers in production. Until cd-platform#179 points CdApiService at
    # cd.api directly, a change resolvers depend on needs #179 first.
    return CdApiService(LambdaApiClient(settings.CD_API_FUNCTION_NAME))
