"""Stores and serves back AI-generated summaries -- general-purpose, not
voting-record-specific (see migrations/versions/0002_ai_summaries.py's
own docstring on `kind`/`subject`). AiSummaryClient is the storage half
(thin, owns the ai_summaries connection pool and raw SQL, no prompt/
Bedrock knowledge -- same role/shape as UsersClient). AiSummaryService is
where the two live: a kind-agnostic history() read path, plus the one
kind this repo actually generates today, voting-record summaries
(generate_voting_record_summary() -- prompt composition + the Bedrock
Converse call + persisting the result). A future kind (e.g. a bill's
legislative evolution) gets its own generate_*() method here, not a
generic one -- each kind's prompt/inputs are different enough that a
shared "generate" abstraction would just be indirection.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from cd.server import settings
from cd.server.services.bedrock_chat_service import BedrockChatClient, get_bedrock_chat_client
from cd.server.services.bill_search_service import BillResult, BillSearchService
from cd.server.services.cd_api_service import CdApiService


@dataclass(frozen=True)
class AiSummaryRecord:
    """One stored row -- what AiSummaryService hands back to schema.py,
    whether freshly generated or read from history. `subject` is
    kind-specific (e.g. {"bioguideId", "topic", "bills": [...]} for
    kind="voting_record") -- callers that care about one kind read it
    out of there, not off this record directly."""

    id: int
    kind: str
    subject: Any  # kind-specific JSON (dict) -- see AiSummaryClient's jsonb codec
    prompt_template: str  # the system-prompt text this row was generated with
    summary: str
    model_id: str
    created_at: datetime


def _record(row: asyncpg.Record) -> AiSummaryRecord:
    return AiSummaryRecord(
        id=row["ai_summary_id"],
        kind=row["kind"],
        # Already a parsed Python object, not raw JSON text -- the pool's
        # jsonb codec (registered in AiSummaryClient.connect()) decodes
        # it automatically.
        subject=row["subject"],
        prompt_template=row["prompt_template"],
        summary=row["summary"],
        model_id=row["model_id"],
        created_at=row["created_at"],
    )


class AiSummaryClient:
    """Thin wrapper around the ai_summaries connection pool -- owns the
    raw insert/select SQL, no prompt/Bedrock/kind-specific knowledge.
    Its own independent asyncpg.Pool against cd_customers, following the
    "each service opens its own pool, no sharing" precedent UsersClient/
    CdApiService/GeocoderService already set. connect()/close() are
    designed to be called from app.py's lifespan (asyncpg.create_pool()
    is a coroutine, can't run at construction time), same as UsersClient
    -- but that wiring isn't added until this service is actually
    instantiated at module scope in a follow-up PR; nothing calls
    connect() yet in this one."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # Registered per-connection (asyncpg's init= runs once for every
        # new physical connection the pool opens, not once for the pool
        # itself) so `subject` binds/reads as a plain Python object --
        # json.dumps()/loads() + an explicit ::jsonb cast are otherwise
        # needed on every call, since asyncpg has no jsonb codec by
        # default. schema="pg_catalog" matches the built-in jsonb type,
        # not some user-defined type of the same name.
        self._pool = await asyncpg.create_pool(self._dsn, init=self._register_codecs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @staticmethod
    async def _register_codecs(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def insert_summary(
        self,
        user_id: str,
        kind: str,
        subject: Any,
        prompt_template: str,
        summary: str,
        model_id: str,
    ) -> AiSummaryRecord:
        assert self._pool is not None, "AiSummaryClient.connect() was never called"
        # RETURNING only the two DB-generated columns, not every value
        # the caller already passed in -- subject in particular can be a
        # full bills+votes JSON snapshot (see the migration's own
        # docstring), not worth a round-trip re-select/re-decode of data
        # this call just wrote verbatim.
        row = await self._pool.fetchrow(
            """
            INSERT INTO ai_summaries (
                user_id, kind, subject, prompt_template, summary, model_id
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING ai_summary_id, created_at
            """,
            user_id,
            kind,
            # Bound directly as a Python object -- the pool's jsonb codec
            # (see _register_codecs) encodes it; Postgres infers $3's
            # type as jsonb from the target column, no explicit cast
            # needed.
            subject,
            prompt_template,
            summary,
            model_id,
        )
        return AiSummaryRecord(
            id=row["ai_summary_id"],
            kind=kind,
            subject=subject,
            prompt_template=prompt_template,
            summary=summary,
            model_id=model_id,
            created_at=row["created_at"],
        )

    async def fetch_history(self, user_id: str, limit: int) -> list[AiSummaryRecord]:
        assert self._pool is not None, "AiSummaryClient.connect() was never called"
        rows = await self._pool.fetch(
            """
            SELECT ai_summary_id, kind, subject, prompt_template, summary, model_id, created_at
            FROM ai_summaries
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return [_record(row) for row in rows]


# The exact, validated system prompt for kind="voting_record" -- iterated
# on and tested against a real searchBills(bioguideId, q) response before
# landing here; treat as fixed text, not a place to improvise wording.
# Stored per-row in ai_summaries.prompt_template on generation (see the
# migration's own docstring for why: correlating a later wording change
# against output quality, without needing a prompt_templates table yet).
_VOTING_RECORD_SYSTEM_PROMPT = """\
You are a neutral, nonpartisan legislative-research assistant. You summarize
a member of Congress's voting record using ONLY the JSON data provided in
the user message — no outside knowledge about the member, the bills, or the
topic. If the data doesn't support a claim, don't make it.

Field meanings you must respect:
- `matches[].via`: how this bill matched the search topic. "policy_area" or
  "subject" means an exact controlled-vocabulary match (high confidence).
  "summary" means semantic similarity to the bill's CRS summary (lower
  confidence — the bill may only be tangentially related; check the title
  and summary yourself before treating it as on-topic).
- `votes`: this member's cast positions on this bill. An EMPTY array means
  the bill matched the search topic but the member has NO recorded vote on
  it — state this explicitly per bill, never omit the bill or invent a vote.
- `voteQuestion`: distinguish procedural questions (e.g. "On Motion to
  Recommit", "On Ordering the Previous Question") from substantive ones
  (e.g. "On Passage", "On the Resolution"). A motion to recommit is
  typically a parliamentary tactic — often used to try to amend or kill a
  bill before final passage — NOT a vote on the bill's content. Never
  describe a motion-to-recommit vote as if it were a vote on the bill
  itself; report it as its own line with its own label.

Output format — be terse. One line per bill, no restated CRS text beyond
a 5-8 word gloss. State each rule (MTR = procedural, "summary" match =
lower confidence) ONCE, up top, not per bill:

1. One sentence: overall pattern on substantive votes only.
2. One line per bill: "<vote> — <bill id>, <title> (<5-8 word gloss>), <date>"
   Tag with "*" if via=summary and the title suggests it's off-topic.
3. One line noting any procedural (MTR) votes exist and how they went,
   without repeating them per bill.
4. One line: data covers only House-voted bills matching by similarity.

Do not speculate about motive. Do not characterize the member's overall
stance on the topic as a value judgment (e.g. "supports/opposes X rights")
— describe only the specific legislative actions in the data.\
"""


def _bill_to_json(bill: BillResult) -> dict[str, Any]:
    # The exact shape searchBills returns over GraphQL (camelCase keys) --
    # the prompt was designed and validated against that response, and
    # this doubles as ai_summaries.subject's stored snapshot (see the
    # migration's own docstring on its dual purpose), so it needs to
    # match byte-for-byte, not just carry equivalent information.
    return {
        "billKey": bill.bill_key,
        "congress": bill.congress,
        "billType": bill.bill_type,
        "billNumber": bill.bill_number,
        "title": bill.title,
        "policyArea": bill.policy_area,
        "crsSummary": bill.crs_summary,
        "matches": bill.matches,
        "votes": [
            {
                "voteCast": vote.vote_cast,
                "voteQuestion": vote.vote_question,
                "result": vote.result,
                "voteDate": vote.vote_date.isoformat(),
            }
            for vote in bill.votes
        ],
    }


def _display_name(first_name: str | None, last_name: str | None, bioguide_id: str) -> str:
    # Simplest correct name for prompt substitution -- nickname/suffix
    # composition can be added later if a generated summary reads oddly
    # for a member who goes by one; not load-bearing for correctness
    # today. Falls back to the bioguide id in the (effectively never, for
    # a sitting/current-Congress member) case both name fields are null,
    # rather than producing "None None".
    name = " ".join(part for part in (first_name, last_name) if part)
    return name or bioguide_id


def _build_user_prompt(member_name: str, topic: str, bills_json: list[dict[str, Any]]) -> str:
    payload = json.dumps(bills_json, indent=2)
    return f'Summarize {member_name}\'s voting record on "{topic}" using this data:\n\n{payload}'


class AiSummaryService:
    """What schema.py's resolvers depend on."""

    def __init__(
        self,
        client: AiSummaryClient,
        cd_api: CdApiService,
        bill_search: BillSearchService,
        chat: BedrockChatClient,
    ):
        self._client = client
        self._cd_api = cd_api
        self._bill_search = bill_search
        self._chat = chat

    async def connect(self) -> None:
        await self._client.connect()

    async def aclose(self) -> None:
        await self._client.close()

    async def history(self, user_id: str, limit: int = 20) -> list[AiSummaryRecord]:
        return await self._client.fetch_history(user_id, limit)

    async def generate_voting_record_summary(
        self, user_id: str, bioguide_id: str, topic: str, limit: int = 10
    ) -> AiSummaryRecord:
        """Resolves the member's display name and this topic's matched
        bills+votes (the same data searchBills(bioguideId, topic, limit)
        itself returns) concurrently, composes the validated prompt,
        calls Bedrock, and stores the result. Always generates fresh --
        no caching/dedup of repeat identical requests, so every call
        produces a new row (see the migration's own docstring on why
        that matters for usage insight).

        An unknown bioguide_id surfaces as a GraphQL error once wired up
        (cd-api 404, from either the member-detail or the votes hop); a
        Bedrock outage surfaces as BedrockConverseError. Neither is
        caught here -- propagates raw to the caller, same "let it
        propagate" style the rest of this schema uses.
        """
        # return_exceptions=True so a failure in one hop doesn't leave the
        # other running as an orphan whose exception is never retrieved
        # (asyncio logs "Task exception was never retrieved" for that, and
        # the wasted cd-api calls keep going). gather waits for both, then
        # we re-raise the first failure with its own type -- an
        # asyncio.TaskGroup would cancel the sibling but wrap the error in
        # an ExceptionGroup, changing what schema.py's resolver re-raises.
        member_doc, bills = await asyncio.gather(
            self._cd_api.member_detail(bioguide_id),
            self._bill_search.search(bioguide_id, topic, limit),
            return_exceptions=True,
        )
        for result in (member_doc, bills):
            if isinstance(result, BaseException):
                raise result

        member_name = _display_name(
            member_doc.data.attributes.first_name,
            member_doc.data.attributes.last_name,
            bioguide_id,
        )
        bills_json = [_bill_to_json(bill) for bill in bills]
        subject = {"bioguideId": bioguide_id, "topic": topic, "bills": bills_json}
        user_prompt = _build_user_prompt(member_name, topic, bills_json)

        summary_text = await self._chat.converse(_VOTING_RECORD_SYSTEM_PROMPT, user_prompt)

        return await self._client.insert_summary(
            user_id,
            "voting_record",
            subject,
            _VOTING_RECORD_SYSTEM_PROMPT,
            summary_text,
            self._chat.model_id,
        )


def get_ai_summary_service(
    cd_api: CdApiService, bill_search: BillSearchService
) -> AiSummaryService:
    return AiSummaryService(
        AiSummaryClient(settings.PG_DSN), cd_api, bill_search, get_bedrock_chat_client()
    )
