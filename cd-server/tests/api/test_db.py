import datetime
import uuid

import pytest
from conftest import random_number

from cd.api import db

# cd.api.db is async (asyncpg). anyio's pytest plugin runs each test and
# its fixtures on one event loop -- the loop the pool below is bound to.
pytestmark = pytest.mark.anyio

CONGRESS = 119

# Derived, not hard-coded: the view treats end_year >= the server's
# current year as "still in office", so a "departed" fixture year must
# track the wall clock.
LAST_YEAR = datetime.date.today().year - 1


@pytest.fixture(autouse=True)
async def pool(pg_conn):
    # Opened per test because an asyncpg pool belongs to the event loop
    # that created it. Depends on pg_conn so every test here skips cleanly
    # when Postgres isn't reachable.
    await db.connect()
    yield
    await db.close()


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


def _insert_member(
    pg_conn,
    bioguide_id: str,
    given_name: str = "Test",
    family_name: str = "Member",
    nickname: str | None = None,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO members (bioguide_id, given_name, family_name, nickname, source_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (bioguide_id, given_name, family_name, nickname, f"hash-{bioguide_id}"),
        )


def _insert_term(
    pg_conn,
    bioguide_id: str,
    state: str = "ZZ",
    end_year: int | None = None,
    congress: int = 119,
) -> None:
    # A HOUSE term. Defaults to the current Congress (119, seeded by
    # migration 0001). end_year=None -> still serving; a past year ->
    # left mid-term (in_office should be false).
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, end_year, source_hash
            ) VALUES (%s, %s, 'HOUSE', 'Representative', %s, 7, 2023, %s, %s)
            """,
            (bioguide_id, congress, state, end_year, f"hash-term-{bioguide_id}"),
        )


def _insert_congress(pg_conn, congress: int, start_date: str, end_date: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO congresses (congress, start_date, end_date) "
            "VALUES (%s, %s, %s) ON CONFLICT (congress) DO NOTHING",
            (congress, start_date, end_date),
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


async def test_fetch_member_returns_a_sitting_member_with_in_office_true(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id)
    _insert_term(pg_conn, bioguide_id, state="GA")
    pg_conn.commit()

    try:
        row = await db.fetch_member(bioguide_id)
        assert row is not None
        assert row["bioguide_id"] == bioguide_id
        assert row["state"] == "GA"
        assert row["member_type"] == "Representative"
        assert row["in_office"] is True
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


async def test_fetch_member_serves_a_departed_current_congress_member(pg_conn):
    # Left the current Congress mid-term -> still served, in_office false.
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id)
    _insert_term(pg_conn, bioguide_id, state="GA", end_year=LAST_YEAR)
    pg_conn.commit()

    try:
        row = await db.fetch_member(bioguide_id)
        assert row is not None
        assert row["bioguide_id"] == bioguide_id
        assert row["in_office"] is False
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


async def test_fetch_member_returns_none_when_only_term_is_a_past_congress(pg_conn):
    # member_terms rows are never deleted, so once the 120th Congress is
    # synced a 119th-only member still has a row -- but fetch_member is
    # scoped (via current_members) to current_congress(), so it 404s.
    # Simulated here with a 118th term while 119 is current.
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_congress(pg_conn, 118, "2023-01-03", "2025-01-03")
    _insert_member(pg_conn, bioguide_id)
    _insert_term(pg_conn, bioguide_id, congress=118)
    pg_conn.commit()

    try:
        assert await db.fetch_member(bioguide_id) is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
            cur.execute("DELETE FROM congresses WHERE congress = 118")
        pg_conn.commit()


async def test_fetch_member_returns_none_for_a_member_with_no_term_at_all(pg_conn):
    # Defensive: a members row with no member_terms row (not a state the
    # ETL produces, but fetch_member shouldn't blow up on it).
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id)
    pg_conn.commit()

    try:
        assert await db.fetch_member(bioguide_id) is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


async def test_fetch_member_returns_none_for_an_unknown_bioguide_id():
    assert await db.fetch_member("NOTAREALID99") is None


def _insert_seat(
    pg_conn,
    bioguide_id: str,
    *,
    chamber: str,
    state: str,
    district: int | None,
    member_type: str | None = None,
    end_year: int | None = None,
    **names: str,
) -> None:
    if member_type is None:
        member_type = "Senator" if chamber == "SENATE" else "Representative"
    _insert_member(pg_conn, bioguide_id, **names)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, end_year, source_hash
            ) VALUES (%s, 119, %s, %s, %s, %s, 2023, %s, %s)
            """,
            (bioguide_id, chamber, member_type, state, district, end_year,
             f"hash-term-{bioguide_id}"),
        )


async def test_fetch_members_returns_senators_and_reps_for_a_state_by_default(pg_conn):
    # A unique 2-char state so leftover "ZZ" rows from other tests in this
    # shared DB can't leak into the result set.
    st = uuid.uuid4().hex[:2].upper()
    other = uuid.uuid4().hex[:2].upper()
    sen_a, sen_b, rep, off = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(4))
    _insert_seat(pg_conn, sen_a, chamber="SENATE", state=st, district=None)
    _insert_seat(pg_conn, sen_b, chamber="SENATE", state=st, district=None)
    _insert_seat(pg_conn, rep, chamber="HOUSE", state=st, district=7)
    _insert_seat(pg_conn, off, chamber="HOUSE", state=other, district=1)
    pg_conn.commit()

    try:
        rows = await db.fetch_members(st)
        assert {r["bioguide_id"] for r in rows} == {sen_a, sen_b, rep}
        # ORDER BY: senators (district NULL) first, then House by district.
        assert rows[-1]["bioguide_id"] == rep
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)",
                        ([sen_a, sen_b, rep, off],))
        pg_conn.commit()


async def test_fetch_members_chamber_filter_excludes_the_other_chamber(pg_conn):
    st = uuid.uuid4().hex[:2].upper()
    sen, rep = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    _insert_seat(pg_conn, sen, chamber="SENATE", state=st, district=None)
    _insert_seat(pg_conn, rep, chamber="HOUSE", state=st, district=3)
    pg_conn.commit()

    try:
        assert [r["bioguide_id"] for r in await db.fetch_members(st, chamber="SENATE")] == [sen]
        assert [r["bioguide_id"] for r in await db.fetch_members(st, chamber="HOUSE")] == [rep]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([sen, rep],))
        pg_conn.commit()


async def test_fetch_members_district_filter_never_bundles_senators(pg_conn):
    # The old union trick returned "district N plus the state's senators".
    # An honest collection returns only House members in district N.
    st = uuid.uuid4().hex[:2].upper()
    sen, rep = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    _insert_seat(pg_conn, sen, chamber="SENATE", state=st, district=None)
    _insert_seat(pg_conn, rep, chamber="HOUSE", state=st, district=5)
    pg_conn.commit()

    try:
        rows = await db.fetch_members(st, district=5)
        assert [r["bioguide_id"] for r in rows] == [rep]
        assert await db.fetch_members(st, district=9) == []  # vacant -> empty, not senators
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([sen, rep],))
        pg_conn.commit()


async def test_fetch_members_excludes_a_departed_member(pg_conn):
    st = uuid.uuid4().hex[:2].upper()
    sitting, gone = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    _insert_seat(pg_conn, sitting, chamber="HOUSE", state=st, district=1)
    _insert_seat(pg_conn, gone, chamber="HOUSE", state=st, district=2, end_year=LAST_YEAR)
    pg_conn.commit()

    try:
        assert [r["bioguide_id"] for r in await db.fetch_members(st)] == [sitting]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([sitting, gone],))
        pg_conn.commit()


def _unique_name() -> str:
    # A family name no other row in the shared test DB can contain.
    return f"Zq{uuid.uuid4().hex[:10]}"


async def test_fetch_members_name_filter_matches_across_states(pg_conn):
    family = _unique_name()
    a, b, other = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(3))
    _insert_seat(pg_conn, a, chamber="SENATE", state="AA", district=None,
                 given_name="Ann", family_name=family)
    _insert_seat(pg_conn, b, chamber="HOUSE", state="BB", district=2,
                 given_name="Ben", family_name=family)
    _insert_seat(pg_conn, other, chamber="HOUSE", state="AA", district=3)
    pg_conn.commit()

    try:
        rows = await db.fetch_members(name=family.upper())  # case-insensitive
        assert [r["bioguide_id"] for r in rows] == [a, b]  # ordered by state
        # Composes with the other filters.
        assert [r["bioguide_id"] for r in await db.fetch_members("BB", name=family)] == [b]
        assert [r["bioguide_id"] for r in await db.fetch_members(name=family, chamber="SENATE")] == [a]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([a, b, other],))
        pg_conn.commit()


async def test_fetch_members_name_filter_needs_every_word_and_checks_the_nickname(pg_conn):
    family = _unique_name()
    member = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_seat(pg_conn, member, chamber="SENATE", state="AA", district=None,
                 given_name="Bernard", nickname="Bernie", family_name=family)
    pg_conn.commit()

    try:
        for query in (f"bernie {family}", f"Bernard {family}", family[:6]):
            assert [r["bioguide_id"] for r in await db.fetch_members(name=query)] == [member]
        assert await db.fetch_members(name=f"Alice {family}") == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (member,))
        pg_conn.commit()


async def test_fetch_members_name_filter_matches_like_wildcards_literally(pg_conn):
    family = _unique_name()
    member = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_seat(pg_conn, member, chamber="HOUSE", state="AA", district=1,
                 family_name=family)
    pg_conn.commit()

    try:
        # Unescaped, "%" or "_" would match this member's name.
        assert await db.fetch_members(name=f"{family[:4]}%") == []
        assert await db.fetch_members(name=f"{family[:4]}_") == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (member,))
        pg_conn.commit()


async def test_fetch_closest_vocab_term_returns_the_nearest_match(pg_conn):
    kind = "POLICY_AREA"
    close_term, far_term = (f"test-term-{uuid.uuid4().hex[:8]}" for _ in range(2))
    _insert_vocab_term(pg_conn, kind, close_term, _vector(1.0, 0.0))
    _insert_vocab_term(pg_conn, kind, far_term, _vector(0.0, 1.0))
    pg_conn.commit()

    try:
        result = await db.fetch_closest_vocab_term(_vector(1.0, 0.0))

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


async def test_fetch_bills_by_policy_area_matches_exact_term(pg_conn):
    bill_number = _bill_number()
    bill_id = _insert_bill(pg_conn, bill_number, policy_area="Immigration")
    pg_conn.commit()

    try:
        results = await db.fetch_bills_by_policy_area("Immigration", limit=10)

        matched = next(row for row in results if row["bill_id"] == bill_id)
        assert matched["bill_key"] == f"119-hr-{bill_number}"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


async def test_fetch_bills_by_policy_area_respects_limit(pg_conn):
    term = f"test-policy-area-{uuid.uuid4().hex[:8]}"
    bill_ids = [_insert_bill(pg_conn, _bill_number(), policy_area=term) for _ in range(3)]
    pg_conn.commit()

    try:
        results = await db.fetch_bills_by_policy_area(term, limit=2)

        assert len(results) == 2
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", (bill_ids,))
        pg_conn.commit()


async def test_fetch_bills_by_subject_matches_a_joined_subject_name(pg_conn):
    term = f"test-subject-{uuid.uuid4().hex[:8]}"
    bill_id = _insert_bill(pg_conn, _bill_number())
    _insert_subject(pg_conn, bill_id, term)
    pg_conn.commit()

    try:
        results = await db.fetch_bills_by_subject(term, limit=10)

        assert [row["bill_id"] for row in results] == [bill_id]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


async def test_fetch_bills_by_similarity_orders_closest_first(pg_conn):
    closest = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    medium = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 1.0))
    farthest = _insert_bill(pg_conn, _bill_number(), embedding=_vector(0.0, 1.0))
    pg_conn.commit()

    try:
        # max_distance=2.0 (cosine distance's own max) -- this test cares
        # about ordering, not the relevance-floor filtering covered below.
        results = await db.fetch_bills_by_similarity(
            _vector(1.0, 0.0), exclude_bill_ids=[], limit=10, max_distance=2.0,
        )
        result_ids = [row["bill_id"] for row in results]

        assert result_ids.index(closest) < result_ids.index(medium) < result_ids.index(farthest)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bills WHERE bill_id = ANY(%s)", ([closest, medium, farthest],),
            )
        pg_conn.commit()


async def test_fetch_bills_by_similarity_excludes_given_bill_ids(pg_conn):
    excluded = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    included = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    pg_conn.commit()

    try:
        results = await db.fetch_bills_by_similarity(
            _vector(1.0, 0.0), exclude_bill_ids=[excluded], limit=10, max_distance=2.0,
        )
        result_ids = {row["bill_id"] for row in results}

        assert excluded not in result_ids
        assert included in result_ids
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", ([excluded, included],))
        pg_conn.commit()


async def test_fetch_bills_by_similarity_excludes_bills_with_no_embedding(pg_conn):
    bill_id = _insert_bill(pg_conn, _bill_number(), embedding=None)
    pg_conn.commit()

    try:
        results = await db.fetch_bills_by_similarity(
            _vector(1.0, 0.0), exclude_bill_ids=[], limit=1000, max_distance=2.0,
        )

        assert bill_id not in {row["bill_id"] for row in results}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
        pg_conn.commit()


async def test_fetch_bills_by_similarity_excludes_bills_beyond_max_distance(pg_conn):
    # The relevance floor: a bill farther than max_distance from the
    # query embedding is excluded entirely, not backfilled in just to
    # pad the response out to `limit` -- pins the fix for a query with
    # no genuinely related bill in the corpus otherwise always returning
    # `limit` results anyway.
    near = _insert_bill(pg_conn, _bill_number(), embedding=_vector(1.0, 0.0))
    far = _insert_bill(pg_conn, _bill_number(), embedding=_vector(0.0, 1.0))
    pg_conn.commit()

    try:
        # cosine distance(near, query) == 0.0, distance(far, query) == 1.0
        results = await db.fetch_bills_by_similarity(
            _vector(1.0, 0.0), exclude_bill_ids=[], limit=10, max_distance=0.5,
        )
        result_ids = {row["bill_id"] for row in results}

        assert near in result_ids
        assert far not in result_ids
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", ([near, far],))
        pg_conn.commit()


async def test_fetch_member_votes_returns_the_members_votes_keyed_by_bill_key(pg_conn):
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    bill_number = _bill_number()
    bill_id = _insert_bill(pg_conn, bill_number)
    _insert_member(pg_conn, voter)
    _insert_term(pg_conn, voter)  # fetch_member_votes 404s a non-current-Congress id
    pg_conn.commit()
    _insert_vote(pg_conn, bill_id, _vote_number(), voter, vote_cast="YEA")
    pg_conn.commit()

    try:
        rows = await db.fetch_member_votes(voter, [f"119-hr-{bill_number}"])

        assert len(rows) == 1
        assert rows[0]["bill_key"] == f"119-hr-{bill_number}"
        # The roll call's natural-key parts, for building the roll_call id.
        assert rows[0]["chamber"] == "HOUSE"
        assert rows[0]["congress"] == 119
        assert rows[0]["session"] == 1
        assert rows[0]["vote_number"] is not None
        assert rows[0]["vote_cast"] == "YEA"
        assert rows[0]["vote_question"] == "On Passage"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM roll_calls WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()


async def test_fetch_member_votes_keeps_a_synced_bill_with_no_member_vote(pg_conn):
    # A bill that exists and has a roll call, but not one this member
    # voted on -> one row, NULL vote_cast (the LEFT JOIN miss). The
    # shaper routes this to meta.bills_without_votes, not a resource.
    voter, other = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    bill_number = _bill_number()
    bill_id = _insert_bill(pg_conn, bill_number)
    _insert_member(pg_conn, voter)
    _insert_term(pg_conn, voter)
    _insert_member(pg_conn, other)
    pg_conn.commit()
    _insert_vote(pg_conn, bill_id, _vote_number(), other, vote_cast="NAY")
    pg_conn.commit()

    try:
        rows = await db.fetch_member_votes(voter, [f"119-hr-{bill_number}"])

        assert len(rows) == 1
        assert rows[0]["bill_key"] == f"119-hr-{bill_number}"
        assert rows[0]["vote_cast"] is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM roll_calls WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([voter, other],))
        pg_conn.commit()


async def test_fetch_member_votes_keeps_a_synced_bill_with_no_roll_calls_at_all(pg_conn):
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    bill_number = _bill_number()
    bill_id = _insert_bill(pg_conn, bill_number)
    _insert_member(pg_conn, voter)
    _insert_term(pg_conn, voter)
    pg_conn.commit()

    try:
        rows = await db.fetch_member_votes(voter, [f"119-hr-{bill_number}"])

        assert len(rows) == 1
        assert rows[0]["vote_cast"] is None
        assert rows[0]["vote_question"] is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()


async def test_fetch_member_votes_omits_a_bill_key_that_matches_no_bill(pg_conn):
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, voter)
    _insert_term(pg_conn, voter)
    pg_conn.commit()

    try:
        rows = await db.fetch_member_votes(voter, ["119-hr-99999999"])
        assert rows == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()


async def test_fetch_member_votes_returns_none_for_a_non_current_congress_member(pg_conn):
    # No current-Congress term -> None (the route's 404), same rule as
    # fetch_member. A bare members row with no member_terms row stands in.
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, voter)
    pg_conn.commit()

    try:
        assert await db.fetch_member_votes(voter, ["119-hr-1"]) is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()


async def test_fetch_member_votes_returns_none_for_an_unknown_bioguide_id():
    assert await db.fetch_member_votes("NOTAREALID99", ["119-hr-1"]) is None


async def test_fetch_member_votes_returns_votes_oldest_first(pg_conn):
    voter = f"TEST{uuid.uuid4().hex[:8].upper()}"
    bill_number = _bill_number()
    bill_id = _insert_bill(pg_conn, bill_number)
    _insert_member(pg_conn, voter)
    _insert_term(pg_conn, voter)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        for vote_date, question in (
            ("2026-05-20", "On Passage"),
            ("2026-05-18", "On Motion to Recommit"),
        ):
            cur.execute(
                """
                INSERT INTO roll_calls (
                    chamber, congress, session, vote_number, bill_id,
                    vote_question, result, vote_date, source_hash
                ) VALUES ('HOUSE', %s, 1, %s, %s, %s, 'Passed', %s, %s)
                RETURNING roll_call_id
                """,
                (CONGRESS, _vote_number(), bill_id, question, vote_date,
                 f"hash-{question}-{bill_number}"),
            )
            rc = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO roll_call_member_votes (roll_call_id, bioguide_id, vote_cast) "
                "VALUES (%s, %s, 'YEA')",
                (rc, voter),
            )
    pg_conn.commit()

    try:
        rows = await db.fetch_member_votes(voter, [f"119-hr-{bill_number}"])

        assert [r["vote_question"] for r in rows] == [
            "On Motion to Recommit", "On Passage"
        ]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM roll_calls WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (voter,))
        pg_conn.commit()


async def test_rows_are_plain_mutable_dicts(pg_conn):
    # Not asyncpg.Record: bills.py adds a "matches" key to each row.
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id)
    _insert_term(pg_conn, bioguide_id)
    pg_conn.commit()

    try:
        row = await db.fetch_member(bioguide_id)
        assert type(row) is dict
        rows = await db.fetch_members("ZZ")
        assert all(type(r) is dict for r in rows)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


async def test_pool_reuses_its_connections_across_queries():
    # Ten sequential queries must land on at most POOL_SIZE backends -- a
    # pool that reconnected per query would show ten distinct pids.
    pids = set()
    for _ in range(10):
        async with db._acquire() as conn:
            pids.add(await conn.fetchval("SELECT pg_backend_pid()"))
    assert len(pids) <= db.POOL_SIZE


async def test_a_query_before_connect_raises():
    await db.close()

    with pytest.raises(RuntimeError, match="connect"):
        await db.fetch_member("Z999999")


async def test_close_then_connect_reopens_the_pool():
    # Each TestClient lifespan in the suite closes and reopens this pool,
    # so close() must leave it fully reset.
    await db.close()
    assert db._pool is None

    await db.connect()
    assert await db.fetch_member("Z999999") is None


async def test_connect_survives_an_unreachable_database(monkeypatch, caplog):
    # An unreachable congressional_app must not fail cd-server's boot (that
    # would take GraphQL and /health down too) -- only warn, then fail /v1.
    await db.close()
    monkeypatch.setitem(db.PG_DSN, "port", "1")  # nothing listens on port 1

    await db.connect()

    assert "can't reach its database" in caplog.text
    with pytest.raises(OSError):
        await db.fetch_member("Z999999")


async def test_connect_twice_keeps_the_open_pool():
    # A second connect() must not replace (and orphan) the open pool.
    pool = db._pool
    await db.connect()
    assert db._pool is pool
