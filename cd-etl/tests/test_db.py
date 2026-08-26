from cd.etl import db


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

    txn = db.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
    with txn as entered_conn:
        assert entered_conn is conn

    assert conn.committed
    assert not conn.rolled_back
    assert conn.closed
    assert txn.failed is False


def test_isolated_transaction_rolls_back_and_suppresses_on_failure():
    conn = _FakeIsolatedConn()

    txn = db.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
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

    txn = db.IsolatedTransaction(_FakeIsolatedHook(conn), "test unit")
    with txn:
        raise RuntimeError("boom")

    assert conn.closed
    assert txn.failed is True


def test_source_hash_is_order_and_case_and_whitespace_insensitive_per_field():
    a = db.source_hash("Foo", " Bar ", None, 1)
    b = db.source_hash("foo", "bar", "", 1)

    assert a == b


def test_source_hash_differs_when_a_field_actually_differs():
    a = db.source_hash("foo", "bar")
    b = db.source_hash("foo", "baz")

    assert a != b
