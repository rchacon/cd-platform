import asyncio

import asyncpg
import pytest

from cd.server.db import CustomersDb, _register_codecs

_SUBJECT = {"bioguideId": "K000401", "bills": [{"billKey": "119-hr-2616", "votes": []}]}


def test_register_codecs_sets_the_jsonb_codec():
    calls = []

    class _FakeConn:
        async def set_type_codec(self, type_name, *, encoder, decoder, schema):
            calls.append((type_name, encoder, decoder, schema))

    asyncio.run(_register_codecs(_FakeConn()))

    assert len(calls) == 1
    type_name, encoder, decoder, schema = calls[0]
    assert type_name == "jsonb"
    assert schema == "pg_catalog"
    # encoder/decoder round-trip a Python object through JSON text -- what
    # lets AiSummaryClient bind/read subject as a plain object.
    assert decoder(encoder(_SUBJECT)) == _SUBJECT


class _ProbePool:
    def __init__(self, fails=False):
        self.fails = fails
        self.probes = 0
        self.closed = False

    async def fetchval(self, query):
        assert query == "SELECT 1"
        self.probes += 1
        if self.fails:
            raise OSError("password authentication failed")
        return 1

    async def close(self):
        self.closed = True


def test_connect_opens_one_on_demand_pool_with_the_codec_hook(monkeypatch):
    calls = []
    pool = _ProbePool()

    async def fake_create_pool(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    db = CustomersDb("postgresql://ignored", max_size=7)

    asyncio.run(db.connect())
    asyncio.run(db.connect())  # already open: no second pool

    assert calls == [
        (
            "postgresql://ignored",
            {
                "min_size": 0,
                "max_size": 7,
                "timeout": 10,
                "command_timeout": 15,
                "max_inactive_connection_lifetime": 300,
                "init": _register_codecs,
            },
        )
    ]
    assert db.pool is pool
    assert pool.probes == 1


def test_connect_fails_and_closes_the_pool_when_the_probe_fails(monkeypatch):
    pool = _ProbePool(fails=True)

    async def fake_create_pool(dsn, **kwargs):
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    db = CustomersDb("postgresql://ignored")

    with pytest.raises(OSError, match="password authentication failed"):
        asyncio.run(db.connect())

    # Raising fails the lifespan, so a misconfigured task never starts
    # serving; the half-open pool is closed, and connect() can retry.
    assert pool.closed
    with pytest.raises(RuntimeError):
        db.pool


def test_pool_before_connect_raises():
    with pytest.raises(RuntimeError, match="connect\\(\\) was never called"):
        CustomersDb("postgresql://ignored").pool


def test_close_closes_the_pool_and_allows_reconnecting(monkeypatch):
    closed = []

    class _FakePool:
        async def fetchval(self, query):
            return 1

        async def close(self):
            closed.append(True)

    async def fake_create_pool(dsn, **kwargs):
        return _FakePool()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    db = CustomersDb("postgresql://ignored")

    asyncio.run(db.connect())
    asyncio.run(db.close())
    asyncio.run(db.close())  # idempotent

    assert closed == [True]
    with pytest.raises(RuntimeError):
        db.pool
