import threading

import bills_etl as etl


class _FakeExtractHook:
    def __init__(self, known_bills):
        self._known_bills = known_bills
        self.calls = []

    def get_records(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return self._known_bills


class _FakeConn:
    def __init__(self):
        self.rolled_back_count = 0
        self.closed = False

    def rollback(self):
        self.rolled_back_count += 1

    def close(self):
        self.closed = True


class _FakeConnHook:
    # Returns a distinct _FakeConn per get_conn() call, same as a real
    # PostgresHook handing each concurrent worker its own connection --
    # refresh_bills relies on this (sync_bill's cursor/commit calls
    # aren't safe to share across threads on one connection).
    def __init__(self):
        self.connections = []
        self._lock = threading.Lock()

    def get_conn(self):
        conn = _FakeConn()
        with self._lock:
            self.connections.append(conn)
        return conn


def test_extract_known_bills_returns_known_bills_for_congress(monkeypatch):
    monkeypatch.setattr(
        etl, "PostgresHook",
        lambda postgres_conn_id: _FakeExtractHook([("HR", 1), ("S", 2)]),
    )

    dag = etl.bills_etl()
    extract_known_bills = dag.task_dict["extract_known_bills"].python_callable

    result = extract_known_bills(119)

    assert result == [
        {"bill_type": "HR", "bill_number": 1},
        {"bill_type": "S", "bill_number": 2},
    ]


def test_extract_known_bills_applies_the_staleness_cutoff(monkeypatch):
    # Regression test: extract_known_bills must actually pass
    # REFRESH_MIN_INTERVAL_DAYS through to the query, not just define the
    # constant -- pins the staleness backoff this DAG relies on to avoid
    # re-syncing every known bill on every single run.
    fake_hook = _FakeExtractHook([])
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: fake_hook)

    dag = etl.bills_etl()
    extract_known_bills = dag.task_dict["extract_known_bills"].python_callable

    extract_known_bills(119)

    sql, parameters = fake_hook.calls[0]
    assert "synced_at" in sql
    assert parameters == (119, etl.REFRESH_MIN_INTERVAL_DAYS)


def test_refresh_bills_skips_failed_bill_without_failing_the_batch(monkeypatch):
    # Regression-style test, mirrors house_votes_etl's resolve_bills fault
    # isolation: one bill's sync_bill failure shouldn't abort refreshing
    # the rest of the batch, and its own connection must be rolled back
    # so the failure doesn't commit anything partial.
    fake_hook = _FakeConnHook()
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: fake_hook)

    synced = []

    def fake_sync_bill(session, conn, congress, bill_type, bill_number):
        if bill_type == "HR" and bill_number == 1:
            raise RuntimeError("simulated failure")
        synced.append((bill_type, bill_number))

    monkeypatch.setattr(etl.bills_common, "sync_bill", fake_sync_bill)

    dag = etl.bills_etl()
    refresh_bills = dag.task_dict["refresh_bills"].python_callable

    known_bills = [
        {"bill_type": "HR", "bill_number": 1},
        {"bill_type": "S", "bill_number": 2},
    ]

    refresh_bills(known_bills, 119)

    assert synced == [("S", 2)]
    assert len(fake_hook.connections) == 2
    assert all(conn.closed for conn in fake_hook.connections)
    # Exactly one of the two per-worker connections saw the failure.
    assert sorted(conn.rolled_back_count for conn in fake_hook.connections) == [0, 1]


def test_get_current_congress_delegates_to_the_shared_helper(monkeypatch):
    monkeypatch.setattr(
        etl.congress_api, "get_current_congress",
        lambda postgres_conn_id: 119 if postgres_conn_id == etl.POSTGRES_CONN_ID else None,
    )

    dag = etl.bills_etl()
    get_current_congress = dag.task_dict["get_current_congress"].python_callable

    assert get_current_congress() == 119


def test_dag_has_expected_tasks_wired_in_the_expected_order():
    dag = etl.bills_etl()

    assert dag.dag_id == "bills_etl"
    assert set(dag.task_dict.keys()) == {
        "get_current_congress",
        "extract_known_bills",
        "refresh_bills",
    }

    upstream = {
        task_id: set(task.upstream_task_ids)
        for task_id, task in dag.task_dict.items()
    }
    assert upstream["get_current_congress"] == set()
    assert upstream["extract_known_bills"] == {"get_current_congress"}
    assert upstream["refresh_bills"] == {"extract_known_bills", "get_current_congress"}
