import bills_etl as etl


class _FakeExtractHook:
    def __init__(self, known_bills):
        self._known_bills = known_bills

    def get_records(self, sql, parameters=None):
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
    def __init__(self, conn):
        self._conn = conn

    def get_conn(self):
        return self._conn


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


def test_refresh_bills_skips_failed_bill_without_failing_the_batch(monkeypatch):
    # Regression-style test, mirrors house_votes_etl's resolve_bills fault
    # isolation: one bill's sync_bill failure shouldn't abort refreshing
    # the rest of the batch, and the shared connection must be rolled
    # back so the next iteration's queries aren't left in an
    # aborted-transaction state.
    fake_conn = _FakeConn()
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _FakeConnHook(fake_conn))

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
    assert fake_conn.rolled_back_count == 1
    assert fake_conn.closed


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
