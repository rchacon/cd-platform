from cd.etl import http


def test_build_session_retries_transient_failures_get_only():
    session = http.build_session(pool_maxsize=5)
    adapter = session.get_adapter("https://api.congress.gov")

    assert adapter.max_retries.total == 3
    assert adapter.max_retries.allowed_methods == frozenset(["GET"])
    assert set(adapter.max_retries.status_forcelist) == {429, 500, 502, 503, 504}
    assert adapter._pool_maxsize == 5


def test_fetch_concurrently_skips_failures_without_failing_the_batch():
    def fetch_one(id_):
        if id_ == "bad":
            raise RuntimeError("boom")
        return id_.upper()

    results = http.fetch_concurrently(["good1", "bad", "good2"], fetch_one, max_workers=3)

    assert sorted(results) == ["GOOD1", "GOOD2"]
