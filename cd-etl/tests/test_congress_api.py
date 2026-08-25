import importlib

import pytest
from pydantic import BaseModel

from cd.etl import congress_api


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, pages):
        # pages: list of payloads returned in order, one per .get() call
        self._pages = list(pages)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeResponse(self._pages.pop(0))


class Widget(BaseModel):
    name: str
    count: int


def test_build_session_retries_transient_failures_get_only():
    session = congress_api.build_session(pool_maxsize=5)
    adapter = session.get_adapter("https://api.congress.gov")

    assert adapter.max_retries.total == 3
    assert adapter.max_retries.allowed_methods == frozenset(["GET"])
    assert set(adapter.max_retries.status_forcelist) == {429, 500, 502, 503, 504}
    assert adapter._pool_maxsize == 5


def test_api_get_includes_format_alongside_caller_params():
    session = _FakeSession([{"ok": True}])

    result = congress_api.api_get(session, "https://api.congress.gov/v3/thing", {"foo": "bar"})

    assert result == {"ok": True}
    call = session.calls[0]
    assert call["params"]["foo"] == "bar"
    assert call["params"]["format"] == "json"


def test_api_get_sends_the_api_key_as_a_header_not_a_query_param():
    # Regression test: api_key previously traveled as a query param,
    # which meant it ended up embedded in the request URL -- and
    # requests.HTTPError's own string representation includes the full
    # URL, so any failed-request log line leaked the key in plaintext.
    session = _FakeSession([{"ok": True}])

    congress_api.api_get(session, "https://api.congress.gov/v3/thing")

    call = session.calls[0]
    assert call["headers"] == {"X-Api-Key": congress_api._congress_api_key()}
    assert "api_key" not in call["params"]
    assert "api_key" not in (call["url"] or "")


def test_congress_api_key_is_read_lazily_not_at_import_time(monkeypatch):
    # Regression test for cd-platform#79: every DAG file imports this
    # module transitively, so an import-time read broke dag-processor
    # (parses DAG files, never calls the Congress API itself) even
    # though CONGRESS_API_KEY was never set there. importlib.reload()
    # re-executes the module body -- if the read were still at import
    # time, this would raise KeyError instead of succeeding.
    monkeypatch.delenv("CONGRESS_API_KEY", raising=False)
    importlib.reload(congress_api)

    with pytest.raises(KeyError):
        congress_api._congress_api_key()

    monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
    assert congress_api._congress_api_key() == "test-key"


def test_api_get_model_validates_response_into_the_given_model():
    session = _FakeSession([{"name": "widget", "count": 3}])

    result = congress_api.api_get_model(session, "https://api.congress.gov/v3/thing", Widget)

    assert result == Widget(name="widget", count=3)


def test_paginate_stops_on_short_page():
    session = _FakeSession([
        {"items": [{"id": 1}, {"id": 2}]},
    ])

    results = list(congress_api.paginate(
        session, "https://api.congress.gov/v3/thing", {}, items_key="items", page_limit=3,
    ))

    assert results == [{"id": 1}, {"id": 2}]
    assert len(session.calls) == 1


def test_paginate_advances_offset_across_full_pages():
    session = _FakeSession([
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": 3}]},
    ])

    results = list(congress_api.paginate(
        session, "https://api.congress.gov/v3/thing", {}, items_key="items", page_limit=2,
    ))

    assert [item["id"] for item in results] == [1, 2, 3]
    assert session.calls[0]["params"]["offset"] == 0
    assert session.calls[1]["params"]["offset"] == 2


def test_paginate_stops_on_empty_page():
    session = _FakeSession([{"items": []}])

    results = list(congress_api.paginate(
        session, "https://api.congress.gov/v3/thing", {}, items_key="items", page_limit=250,
    ))

    assert results == []
    assert len(session.calls) == 1


def test_fetch_concurrently_skips_failures_without_failing_the_batch():
    def fetch_one(id_):
        if id_ == "bad":
            raise RuntimeError("boom")
        return id_.upper()

    results = congress_api.fetch_concurrently(["good1", "bad", "good2"], fetch_one, max_workers=3)

    assert sorted(results) == ["GOOD1", "GOOD2"]


class _FakeIsolatedConn:
    def __init__(self, fail_rollback=False):
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._fail_rollback = fail_rollback

    def commit(self):
        self.committed = True

    def rollback(self):
        if self._fail_rollback:
            raise RuntimeError("connection already dead")
        self.rolled_back = True

    def close(self):
        self.closed = True


class _FakeIsolatedHook:
    def __init__(self, conn):
        self._conn = conn

    def get_conn(self):
        return self._conn


def test_isolated_transaction_commits_on_success():
    conn = _FakeIsolatedConn()

    txn = congress_api.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
    with txn as entered_conn:
        assert entered_conn is conn

    assert conn.committed
    assert not conn.rolled_back
    assert conn.closed
    assert txn.failed is False


def test_isolated_transaction_rolls_back_and_suppresses_on_failure():
    conn = _FakeIsolatedConn()

    txn = congress_api.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
    with txn:
        raise RuntimeError("boom")  # must not propagate past the `with`

    assert not conn.committed
    assert conn.rolled_back
    assert conn.closed
    assert txn.failed is True


def test_isolated_transaction_swallows_a_dead_connections_rollback_failure():
    # Regression test: rollback() on an already-dead connection must not
    # itself escape uncaught -- that would defeat the whole point of
    # isolating one unit's failure from the loop driving this.
    conn = _FakeIsolatedConn(fail_rollback=True)

    txn = congress_api.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
    with txn:
        raise RuntimeError("boom")

    assert conn.closed
    assert txn.failed is True


def test_source_hash_is_order_and_case_and_whitespace_insensitive_per_field():
    a = congress_api.source_hash("Foo", " Bar ", None, 1)
    b = congress_api.source_hash("foo", "bar", "", 1)

    assert a == b


def test_source_hash_differs_when_a_field_actually_differs():
    a = congress_api.source_hash("foo", "bar")
    b = congress_api.source_hash("foo", "baz")

    assert a != b
