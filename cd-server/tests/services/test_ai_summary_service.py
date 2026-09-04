import asyncio
from datetime import datetime, timezone

import asyncpg

from cd.server.services.ai_summary_service import (
    AiSummaryClient,
    AiSummaryService,
    get_ai_summary_service,
)

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
    client = _FakeAiSummaryClient()
    service = AiSummaryService(client)

    asyncio.run(service.connect())
    asyncio.run(service.aclose())

    assert client.connected is True
    assert client.closed is True


def test_ai_summary_service_history_delegates_to_the_client():
    sentinel_records = ["a-record"]
    client = _FakeAiSummaryClient(history_result=sentinel_records)
    service = AiSummaryService(client)

    result = asyncio.run(service.history("user-1", limit=5))

    assert client.history_calls == [("user-1", 5)]
    assert result == sentinel_records


def test_ai_summary_service_history_defaults_limit_to_20():
    client = _FakeAiSummaryClient()
    service = AiSummaryService(client)

    asyncio.run(service.history("user-1"))

    assert client.history_calls == [("user-1", 20)]


def test_get_ai_summary_service_returns_a_wired_service():
    service = get_ai_summary_service()
    assert isinstance(service, AiSummaryService)
    assert isinstance(service._client, AiSummaryClient)
