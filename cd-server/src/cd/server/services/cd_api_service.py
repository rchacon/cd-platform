import httpx
from cd.api.app import app as api_app
from cd.lib.jsonapi import CollectionDocument, Document
from cd.lib.models import Bill, Member, MemberDetail, RollCallVote


class ApiClientError(Exception):
    """Raised on a non-2xx response from cd-api's /v1 app, so callers get
    one error type whatever went wrong -- the status code is cd-api's
    own (404 for an unknown member, 503 when search is down, ...)."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"cd-api returned {status_code}: {body}")


def build_cd_api_client() -> httpx.AsyncClient:
    """An httpx client that calls cd.api's FastAPI app in-process, through
    httpx.ASGITransport -- no network hop, no Lambda. It goes through the
    app's routing, request validation and JSON:API serialization exactly
    as a real /v1 request does, so cd-server consumes cd-api's public
    contract the same way cd-lookup does rather than reaching around it
    into cd.api.db/cd.api.transform (cd-platform#179).

    It calls the bare app, not the OriginGuard-wrapped mount in app.py:
    OriginGuard keeps out requests that skipped API Gateway, and this one
    never left the process. Paths are relative to the app (`/members`,
    not `/v1/members`) -- the base URL's host is a placeholder.

    ASGITransport doesn't run the app's lifespan; cd.api's database pool
    is opened by cd-server's own lifespan (app.py).

    An unexpected exception inside a route comes back as the 500 cd.api
    sends any client, which CdApiService raises as ApiClientError(500) --
    cd.api logs the traceback itself (jsonapi.py's JsonApiRoute,
    app.py's catch-all handler). raise_app_exceptions=False because
    Starlette also re-raises the exception after sending that 500, and
    httpx would otherwise surface it raw, outside the one-error-type
    contract.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://cd-api",
    )


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
    """The service layer schema.py depends on for cd-api data. It calls
    cd-api's /v1 routes, validates each response against cd-lib's
    JSON:API models and returns real `Member`/`Bill`/... objects -- the
    response-shape trust boundary lives here, not in schema.py's
    resolvers."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def _get(self, path: str, query: dict[str, str]) -> dict:
        try:
            response = await self._client.get(path, params=query)
        except httpx.HTTPError as e:
            # No response to read a status from. In-process this is rare
            # (it isn't a network call), but a client that does go over
            # the network -- a test's MockTransport, say -- can raise it.
            # 502, as for a gateway whose upstream is unreachable.
            raise ApiClientError(502, str(e)) from e
        if response.is_error:
            raise ApiClientError(response.status_code, response.text)
        return response.json()

    async def get_representatives(self, state: str, district: int) -> list[Member]:
        # One filtered call -- cd-api's honest `/members` collection
        # (cd-platform#104 PR B) narrows to this state's House seat for
        # the district server-side; `_members` re-applies the same filter
        # as a backstop.
        result = await self._get(
            "/members",
            {
                "filter[state]": state,
                "filter[chamber]": "house",
                "filter[district]": str(district),
            },
        )
        return _members(result, chamber="representatives", district=district)

    async def get_senators(self, state: str) -> list[Member]:
        result = await self._get(
            "/members",
            {"filter[state]": state, "filter[chamber]": "senate"},
        )
        return _members(result, chamber="senators")

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
        member) propagates as `ApiClientError`.
        """
        result = await self._get(f"/members/{bioguide_id}", {})
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
        (e.g. `503` when Bedrock is down) propagates as `ApiClientError`.
        """
        params = {"filter[query]": query}
        if page_size is not None:
            params["page[size]"] = str(page_size)
        result = await self._get("/bills", params)
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
        result = await self._get(
            f"/members/{bioguide_id}/votes",
            {"filter[bill]": ",".join(bill_keys)},
        )
        return CollectionDocument[RollCallVote].model_validate(result)

    async def aclose(self) -> None:
        await self._client.aclose()


def get_cd_api_service() -> CdApiService:
    return CdApiService(build_cd_api_client())
