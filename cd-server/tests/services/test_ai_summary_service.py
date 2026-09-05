import asyncio
import types
from datetime import date, datetime, timezone

import asyncpg
import pytest

from cd.server.services.ai_summary_service import (
    AiSummaryClient,
    AiSummaryRecord,
    AiSummaryService,
    get_ai_summary_service,
)
from cd.server.services.bill_search_service import BillResult, VoteResult

_SUBJECT = {
    "bioguideId": "K000401",
    "topic": "immigration",
    "bills": [{"billKey": "119-hr-2616", "title": "...", "votes": []}],
}
_PROMPT_TEMPLATE = "You are a neutral, nonpartisan legislative-research assistant..."

_ROW = {
    "ai_summary_id": 1,
    "kind": "voting_record",
    # The pool's jsonb codec (AiSummaryClient._register_codecs) decodes a
    # jsonb column into a parsed Python object -- the fake row mirrors
    # that, not raw JSON text.
    "subject": _SUBJECT,
    "prompt_template": _PROMPT_TEMPLATE,
    "summary": "Voted NAY on...",
    "model_id": "anthropic.claude-3-5-haiku",
    "created_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
}


class _FakePool:
    def __init__(self, fetchrow_result=None, fetch_result=None):
        self.calls: list[tuple] = []
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result or []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self._fetchrow_result

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self._fetch_result


def test_insert_summary_runs_expected_sql_and_returns_the_record():
    # RETURNING only the two DB-generated columns (see insert_summary's
    # own comment) -- a fetchrow_result with ONLY those two keys proves
    # the record is built from the caller's own arguments, not by
    # re-reading kind/subject/etc. off the row (which would KeyError
    # here if it tried).
    client = AiSummaryClient("postgresql://ignored")
    client._pool = _FakePool(
        fetchrow_result={"ai_summary_id": 1, "created_at": _ROW["created_at"]}
    )

    record = asyncio.run(
        client.insert_summary(
            "user-1",
            "voting_record",
            _SUBJECT,
            _PROMPT_TEMPLATE,
            "Voted NAY on...",
            "anthropic.claude-3-5-haiku",
        )
    )

    _, query, args = client._pool.calls[0]
    assert "INSERT INTO ai_summaries" in query
    assert "RETURNING ai_summary_id, created_at" in query
    # subject is bound as the raw Python object, no manual json.dumps()
    # or explicit ::jsonb cast -- the pool's jsonb codec (registered in
    # connect(), see _register_codecs) handles the encoding.
    assert args == (
        "user-1",
        "voting_record",
        _SUBJECT,
        _PROMPT_TEMPLATE,
        "Voted NAY on...",
        "anthropic.claude-3-5-haiku",
    )
    assert record.id == 1
    assert record.kind == "voting_record"
    assert record.subject == _SUBJECT
    assert record.prompt_template == _PROMPT_TEMPLATE
    assert record.created_at == _ROW["created_at"]
    assert record.model_id == "anthropic.claude-3-5-haiku"


def test_fetch_history_runs_expected_sql_and_returns_records():
    client = AiSummaryClient("postgresql://ignored")
    client._pool = _FakePool(fetch_result=[_ROW])

    records = asyncio.run(client.fetch_history("user-1", 20))

    _, query, args = client._pool.calls[0]
    assert "kind" in query
    assert "subject" in query
    assert "prompt_template" in query
    assert "WHERE user_id = $1" in query
    assert "ORDER BY created_at DESC" in query
    assert "LIMIT $2" in query
    assert args == ("user-1", 20)
    assert len(records) == 1
    assert records[0].kind == "voting_record"
    assert records[0].subject == _SUBJECT
    assert records[0].prompt_template == _PROMPT_TEMPLATE


def test_fetch_history_returns_empty_list_for_a_user_with_no_history():
    client = AiSummaryClient("postgresql://ignored")
    client._pool = _FakePool(fetch_result=[])

    assert asyncio.run(client.fetch_history("user-1", 20)) == []


def test_fetch_history_mixes_summary_kinds_for_one_user():
    # One History page across every summary type -- fetch_history doesn't
    # filter by kind.
    bill_evolution_row = {
        **_ROW,
        "ai_summary_id": 2,
        "kind": "bill_evolution",
        "subject": {"billKey": "119-hr-2616"},
    }
    client = AiSummaryClient("postgresql://ignored")
    client._pool = _FakePool(fetch_result=[bill_evolution_row, _ROW])

    records = asyncio.run(client.fetch_history("user-1", 20))

    assert [r.kind for r in records] == ["bill_evolution", "voting_record"]


def test_register_codecs_sets_the_jsonb_codec():
    calls = []

    class _FakeConn:
        async def set_type_codec(self, type_name, *, encoder, decoder, schema):
            calls.append((type_name, encoder, decoder, schema))

    asyncio.run(AiSummaryClient._register_codecs(_FakeConn()))

    assert len(calls) == 1
    type_name, encoder, decoder, schema = calls[0]
    assert type_name == "jsonb"
    assert schema == "pg_catalog"
    # encoder/decoder round-trip a Python object through JSON text --
    # this is what lets insert_summary/_record() bind/read subject as a
    # plain object instead of hand-rolling json.dumps()/loads().
    assert decoder(encoder(_SUBJECT)) == _SUBJECT


def test_connect_registers_the_codec_hook_on_the_pool(monkeypatch):
    captured = {}

    async def fake_create_pool(dsn, init=None):
        captured["dsn"] = dsn
        captured["init"] = init
        return "a-pool"

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    client = AiSummaryClient("postgresql://ignored")
    asyncio.run(client.connect())

    assert captured["dsn"] == "postgresql://ignored"
    assert captured["init"] is AiSummaryClient._register_codecs
    assert client._pool == "a-pool"


class _FakeAiSummaryClient:
    def __init__(self, history_result=None):
        self.connected = False
        self.closed = False
        self.history_calls: list[tuple] = []
        self._history_result = history_result if history_result is not None else []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def fetch_history(self, user_id, limit):
        self.history_calls.append((user_id, limit))
        return self._history_result


def test_ai_summary_service_connect_and_aclose_delegate_to_the_client():
    # None for cd_api/bill_search/chat -- connect/aclose/history don't
    # touch them, only generate_voting_record_summary does (see its own
    # tests below).
    client = _FakeAiSummaryClient()
    service = AiSummaryService(client, None, None, None)

    asyncio.run(service.connect())
    asyncio.run(service.aclose())

    assert client.connected is True
    assert client.closed is True


def test_ai_summary_service_history_delegates_to_the_client():
    sentinel_records = ["a-record"]
    client = _FakeAiSummaryClient(history_result=sentinel_records)
    service = AiSummaryService(client, None, None, None)

    result = asyncio.run(service.history("user-1", limit=5))

    assert client.history_calls == [("user-1", 5)]
    assert result == sentinel_records


def test_ai_summary_service_history_defaults_limit_to_20():
    client = _FakeAiSummaryClient()
    service = AiSummaryService(client, None, None, None)

    asyncio.run(service.history("user-1"))

    assert client.history_calls == [("user-1", 20)]


def test_get_ai_summary_service_returns_a_wired_service(monkeypatch):
    from cd.server import settings

    monkeypatch.setattr(settings, "BEDROCK_CHAT_MODEL_ID", "anthropic.claude-3-5-haiku")

    service = get_ai_summary_service(cd_api=object(), bill_search=object())

    assert isinstance(service, AiSummaryService)
    assert isinstance(service._client, AiSummaryClient)


# --- generate_voting_record_summary (prompt composition + Bedrock call) ---

_BILL_WITH_VOTE = BillResult(
    bill_key="119-hr-2616",
    congress=119,
    bill_type="HR",
    bill_number=2616,
    title="A bill about immigration",
    policy_area="Immigration",
    crs_summary="<p>Summary...</p>",
    matches=[{"via": "policy_area"}],
    votes=[
        VoteResult(
            vote_cast="NAY",
            vote_question="On Passage",
            result="Passed",
            vote_date=date(2026, 5, 20),
        )
    ],
)

_BILL_WITHOUT_VOTE = BillResult(
    bill_key="119-s-5",
    congress=119,
    bill_type="S",
    bill_number=5,
    title="A bill without a vote on record",
    policy_area="Immigration",
    crs_summary=None,
    matches=[{"via": "summary"}],
    votes=[],
)


def test_bill_to_json_matches_the_searchbills_shape():
    from cd.server.services.ai_summary_service import _bill_to_json

    payload = _bill_to_json(_BILL_WITH_VOTE)

    assert payload == {
        "billKey": "119-hr-2616",
        "congress": 119,
        "billType": "HR",
        "billNumber": 2616,
        "title": "A bill about immigration",
        "policyArea": "Immigration",
        "crsSummary": "<p>Summary...</p>",
        "matches": [{"via": "policy_area"}],
        "votes": [
            {
                "voteCast": "NAY",
                "voteQuestion": "On Passage",
                "result": "Passed",
                "voteDate": "2026-05-20",
            }
        ],
    }


def test_bill_to_json_keeps_an_empty_votes_list_explicit():
    from cd.server.services.ai_summary_service import _bill_to_json

    assert _bill_to_json(_BILL_WITHOUT_VOTE)["votes"] == []


@pytest.mark.parametrize(
    "first, last, bioguide_id, expected",
    [
        ("Kevin", "Kiley", "K000401", "Kevin Kiley"),
        (None, "Kiley", "K000401", "Kiley"),
        ("Kevin", None, "K000401", "Kevin"),
        (None, None, "K000401", "K000401"),
    ],
)
def test_display_name(first, last, bioguide_id, expected):
    from cd.server.services.ai_summary_service import _display_name

    assert _display_name(first, last, bioguide_id) == expected


def test_build_user_prompt_includes_member_name_topic_and_json_payload():
    from cd.server.services.ai_summary_service import _bill_to_json, _build_user_prompt

    prompt = _build_user_prompt(
        "Kevin Kiley", "immigration", [_bill_to_json(_BILL_WITH_VOTE)]
    )

    assert 'Summarize Kevin Kiley\'s voting record on "immigration"' in prompt
    assert '"billKey": "119-hr-2616"' in prompt


class _FakeCdApi:
    def __init__(self, first_name="Kevin", last_name="Kiley", raises=None):
        self._first_name = first_name
        self._last_name = last_name
        self._raises = raises
        self.calls: list[str] = []

    async def member_detail(self, bioguide_id):
        self.calls.append(bioguide_id)
        if self._raises is not None:
            raise self._raises
        attributes = types.SimpleNamespace(first_name=self._first_name, last_name=self._last_name)
        return types.SimpleNamespace(data=types.SimpleNamespace(attributes=attributes))


class _FakeBillSearch:
    def __init__(self, results, raises=None):
        self._results = results
        self._raises = raises
        self.calls: list[tuple] = []

    async def search(self, bioguide_id, query, page_size):
        self.calls.append((bioguide_id, query, page_size))
        if self._raises is not None:
            raise self._raises
        return self._results


class _FakeChat:
    def __init__(self, text="Voted NAY on...", raises=None):
        self.model_id = "anthropic.claude-3-5-haiku"
        self._text = text
        self._raises = raises
        self.calls: list[tuple] = []

    async def converse(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if self._raises is not None:
            raise self._raises
        return self._text


class _FakeInsertOnlyClient:
    def __init__(self):
        self.insert_calls: list[tuple] = []

    async def insert_summary(self, user_id, kind, subject, prompt_template, summary, model_id):
        self.insert_calls.append((user_id, kind, subject, prompt_template, summary, model_id))
        return AiSummaryRecord(
            id=1,
            kind=kind,
            subject=subject,
            prompt_template=prompt_template,
            summary=summary,
            model_id=model_id,
            created_at=_ROW["created_at"],
        )


def test_generate_voting_record_summary_composes_and_stores_a_fresh_summary():
    cd_api = _FakeCdApi()
    bill_search = _FakeBillSearch([_BILL_WITH_VOTE, _BILL_WITHOUT_VOTE])
    chat = _FakeChat(text="Voted NAY on...")
    client = _FakeInsertOnlyClient()
    service = AiSummaryService(client, cd_api, bill_search, chat)

    record = asyncio.run(
        service.generate_voting_record_summary("user-1", "K000401", "immigration", limit=5)
    )

    # Both hops made with the right args -- run via asyncio.gather in the
    # source (concurrent, not sequential); this asserts the calls
    # happened, not the timing.
    assert cd_api.calls == ["K000401"]
    assert bill_search.calls == [("K000401", "immigration", 5)]

    system_prompt, user_prompt = chat.calls[0]
    assert "neutral, nonpartisan legislative-research assistant" in system_prompt
    assert "Kevin Kiley" in user_prompt
    assert "119-hr-2616" in user_prompt

    assert len(client.insert_calls) == 1
    user_id, kind, subject, prompt_template, summary, model_id = client.insert_calls[0]
    assert user_id == "user-1"
    assert kind == "voting_record"
    assert subject["bioguideId"] == "K000401"
    assert subject["topic"] == "immigration"
    assert [b["billKey"] for b in subject["bills"]] == ["119-hr-2616", "119-s-5"]
    assert subject["bills"][1]["votes"] == []  # matched, no vote on record -- kept explicit
    assert prompt_template == system_prompt
    assert summary == "Voted NAY on..."
    assert model_id == "anthropic.claude-3-5-haiku"

    assert record.summary == "Voted NAY on..."


def test_generate_voting_record_summary_propagates_cd_api_failure():
    from cd.server.services.cd_api_service import ApiClientError

    cd_api = _FakeCdApi(raises=ApiClientError(404, "no current-Congress member"))
    bill_search = _FakeBillSearch([_BILL_WITH_VOTE])
    service = AiSummaryService(_FakeInsertOnlyClient(), cd_api, bill_search, _FakeChat())

    with pytest.raises(ApiClientError):
        asyncio.run(
            service.generate_voting_record_summary("user-1", "X000000", "immigration")
        )


def test_generate_voting_record_summary_propagates_bill_search_failure():
    from cd.server.services.cd_api_service import ApiClientError

    cd_api = _FakeCdApi()
    bill_search = _FakeBillSearch([], raises=ApiClientError(503, "search unavailable"))
    service = AiSummaryService(_FakeInsertOnlyClient(), cd_api, bill_search, _FakeChat())

    with pytest.raises(ApiClientError):
        asyncio.run(service.generate_voting_record_summary("user-1", "K000401", "immigration"))


def test_generate_voting_record_summary_propagates_bedrock_failure():
    from cd.server.services.bedrock_chat_service import BedrockConverseError

    cd_api = _FakeCdApi()
    bill_search = _FakeBillSearch([_BILL_WITH_VOTE])
    chat = _FakeChat(raises=BedrockConverseError("Bedrock Converse call failed"))
    service = AiSummaryService(_FakeInsertOnlyClient(), cd_api, bill_search, chat)

    with pytest.raises(BedrockConverseError):
        asyncio.run(service.generate_voting_record_summary("user-1", "K000401", "immigration"))


def test_generate_voting_record_summary_rejects_an_over_long_topic():
    from cd.server.services.ai_summary_service import _MAX_TOPIC_LEN

    cd_api = _FakeCdApi()
    bill_search = _FakeBillSearch([_BILL_WITH_VOTE])
    chat = _FakeChat()
    service = AiSummaryService(_FakeInsertOnlyClient(), cd_api, bill_search, chat)

    with pytest.raises(ValueError, match="topic must be at most"):
        asyncio.run(
            service.generate_voting_record_summary(
                "user-1", "K000401", "x" * (_MAX_TOPIC_LEN + 1)
            )
        )

    # Rejected before any cd-api/Bedrock work.
    assert cd_api.calls == []
    assert bill_search.calls == []
    assert chat.calls == []


def test_generate_voting_record_summary_raises_a_single_error_when_both_hops_fail():
    # Both concurrent hops fail -- gather(return_exceptions=True) means
    # neither is left as an orphaned task ("Task exception was never
    # retrieved"); one plain exception propagates, not an ExceptionGroup.
    from cd.server.services.cd_api_service import ApiClientError

    cd_api = _FakeCdApi(raises=ApiClientError(404, "no such member"))
    bill_search = _FakeBillSearch([], raises=ApiClientError(503, "search unavailable"))
    service = AiSummaryService(_FakeInsertOnlyClient(), cd_api, bill_search, _FakeChat())

    with pytest.raises(ApiClientError):
        asyncio.run(service.generate_voting_record_summary("user-1", "K000401", "immigration"))
