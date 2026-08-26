import uuid

from conftest import random_number

from cd.api import db

CONGRESS = 119


def _bill_number() -> int:
    # Kept well above any real bill's current range, and under
    # bill_number's SMALLINT max (32767). Matches cd-etl's own
    # tests/test_bills_common.py convention.
    return random_number(20000, 29000)


def _vote_number() -> int:
    return random_number(30000, 39000)


def _vector(*first_values: float, dimensions: int = 1024) -> list[float]:
    # Cosine distance (pgvector's <=> operator) only depends on angle,
    # not magnitude, so these deterministic, un-normalized vectors are
    # enough to test ordering without needing a real Titan embedding.
    values = list(first_values) + [0.0] * (dimensions - len(first_values))
    return values


def _insert_member(pg_conn, bioguide_id: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO members (bioguide_id, given_name, family_name, source_hash) "
            "VALUES (%s, %s, %s, %s)",
            (bioguide_id, "Test", "Member", f"hash-{bioguide_id}"),
        )


def _insert_bill(
    pg_conn,
    bill_number: int,
    bill_type: str = "HR",
    policy_area: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bills (
                congress, bill_type, bill_number, title, policy_area,
                crs_summary, crs_summary_embedding, source_hash
            ) VALUES (
                %(congress)s, %(bill_type)s, %(bill_number)s, %(title)s, %(policy_area)s,
                %(crs_summary)s, %(embedding)s::vector, %(source_hash)s
            )
            RETURNING bill_id
            """,
            {
                "congress": CONGRESS, "bill_type": bill_type, "bill_number": bill_number,
                "title": f"Test Bill {bill_number}", "policy_area": policy_area,
                "crs_summary": "A test bill.",
                "embedding": db._to_pgvector_literal(embedding) if embedding else None,
                "source_hash": f"hash-bill-{bill_number}",
            },
        )
        return cur.fetchone()[0]


def _insert_subject(pg_conn, bill_id: int, subject_name: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bill_subjects (bill_id, subject_name) VALUES (%s, %s)",
            (bill_id, subject_name),
        )


def _insert_vote(
    pg_conn, bill_id: int, vote_number: int, bioguide_id: str, vote_cast: str = "YEA"
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roll_calls (
                chamber, congress, session, vote_number, bill_id,
                vote_question, result, vote_date, source_hash
            ) VALUES (
                'HOUSE', %(congress)s, 1, %(vote_number)s, %(bill_id)s,
                'On Passage', 'Passed', '2025-03-01', %(source_hash)s
            )
            RETURNING roll_call_id
            """,
            {
                "congress": CONGRESS, "vote_number": vote_number, "bill_id": bill_id,
                "source_hash": f"hash-vote-{vote_number}",
            },
        )
        roll_call_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO roll_call_member_votes (roll_call_id, bioguide_id, vote_cast) "
            "VALUES (%s, %s, %s)",
            (roll_call_id, bioguide_id, vote_cast),
        )


def _insert_vocab_term(pg_conn, kind: str, term: str, embedding: list[float]) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vocab_term_embeddings (kind, term, embedding) VALUES (%s, %s, %s::vector)",
            (kind, term, db._to_pgvector_literal(embedding)),
        )


def test_member_exists_true_for_a_real_bioguide_id(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id)
    pg_conn.commit()

    try:
        assert db.member_exists(bioguide_id) is True
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


def test_member_exists_false_for_an_unknown_bioguide_id():
    assert db.member_exists("NOTAREALID99") is False


def test_fetch_closest_vocab_term_returns_the_nearest_match(pg_conn):
    kind = "POLICY_AREA"
    close_term, far_term = (f"test-term-{uuid.uuid4().hex[:8]}" for _ in range(2))
    _insert_vocab_term(pg_conn, kind, close_term, _vector(1.0, 0.0))
    _insert_vocab_term(pg_conn, kind, far_term, _vector(0.0, 1.0))
    pg_conn.commit()

    try:
        result = db.fetch_closest_vocab_term(_vector(1.0, 0.0))

        assert result["term"] == close_term
        assert result["kind"] == kind
        assert result["distance"] < 0.01
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vocab_term_embeddings WHERE term = ANY(%s)",
                ([close_term, far_term],),
            )
        pg_conn.commit()


def test_fetch_bills_by_policy_area_matches_exact_term(pg_conn):
    bill_id = _insert_bill(pg_conn, _bill_number(), policy_area="Immigration")
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_policy_area("Immigration", limit=10)

        assert bill_id in {row["bill_id"] for row in results}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


def test_fetch_bills_by_policy_area_respects_limit(pg_conn):
    term = f"test-policy-area-{uuid.uuid4().hex[:8]}"
    bill_ids = [_insert_bill(pg_conn, _bill_number(), policy_area=term) for _ in range(3)]
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_policy_area(term, limit=2)

        assert len(results) == 2
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", (bill_ids,))
        pg_conn.commit()


def test_fetch_bills_by_subject_matches_a_joined_subject_name(pg_conn):
    term = f"test-subject-{uuid.uuid4().hex[:8]}"
    bill_id = _insert_bill(pg_conn, _bill_number())
    _insert_subject(pg_conn, bill_id, term)
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_subject(term, limit=10)

        assert [row["bill_id"] for row in results] == [bill_id]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


def test_fetch_bills_by_similarity_orders_closest_first(pg_conn):
    closest = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    medium = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 1.0))
    farthest = _insert_bill(pg_conn, _bill_number(), embedding=_vector(0.0, 1.0))
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_similarity(_vector(1.0, 0.0), exclude_bill_ids=[], limit=10)
        result_ids = [row["bill_id"] for row in results]

        assert result_ids.index(closest) < result_ids.index(medium) < result_ids.index(farthest)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bills WHERE bill_id = ANY(%s)", ([closest, medium, farthest],),
            )
        pg_conn.commit()


def test_fetch_bills_by_similarity_excludes_given_bill_ids(pg_conn):
    excluded = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    included = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_similarity(
            _vector(1.0, 0.0), exclude_bill_ids=[excluded], limit=10,
        )
        result_ids = {row["bill_id"] for row in results}

        assert excluded not in result_ids
        assert included in result_ids
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", ([excluded, included],))
        pg_conn.commit()


def test_fetch_bills_by_similarity_excludes_bills_with_no_embedding(pg_conn):
    bill_id = _insert_bill(pg_conn, _bill_number(), embedding=None)
    pg_conn.commit()

    try:
        results = db.fetch_bills_by_similarity(_vector(1.0, 0.0), exclude_bill_ids=[], limit=1000)

        assert bill_id not in {row["bill_id"] for row in results}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


def test_fetch_votes_for_bills_returns_only_the_given_members_votes(pg_conn):
    voter, other = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    bill_id = _insert_bill(pg_conn, _bill_number())
    _insert_member(pg_conn, voter)
    _insert_member(pg_conn, other)
    pg_conn.commit()
    _insert_vote(pg_conn, bill_id, _vote_number(), voter, vote_cast="YEA")
    _insert_vote(pg_conn, bill_id, _vote_number(), other, vote_cast="NAY")
    pg_conn.commit()

    try:
        results = db.fetch_votes_for_bills([bill_id], voter)

        assert len(results) == 1
        assert results[0]["vote_cast"] == "YEA"
        assert results[0]["bill_id"] == bill_id
    finally:
        with pg_conn.cursor() as cur:
            # roll_calls has no ON DELETE CASCADE back to bills (unlike
            # roll_call_member_votes' own cascade back to roll_calls) --
            # must delete it first or bills' delete below hits
            # roll_calls_bill_congress_fk.
            cur.execute("DELETE FROM roll_calls WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([voter, other],))
        pg_conn.commit()


def test_fetch_votes_for_bills_returns_multiple_votes_for_one_bill(pg_conn):
    # A bill can have more than one roll call in a member's own chamber
    # (e.g. a procedural vote plus final passage).
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    bill_id = _insert_bill(pg_conn, _bill_number())
    _insert_member(pg_conn, voter)
    pg_conn.commit()
    _insert_vote(pg_conn, bill_id, _vote_number(), voter)
    _insert_vote(pg_conn, bill_id, _vote_number(), voter)
    pg_conn.commit()

    try:
        results = db.fetch_votes_for_bills([bill_id], voter)

        assert len(results) == 2
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM roll_calls WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()
