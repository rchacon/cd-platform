import pytest

from cd.server.sql import named


def test_names_become_positional_in_first_seen_order():
    assert named(
        "SELECT * FROM t WHERE b = :b AND a = :a", {"a": 1, "b": 2}
    ) == ("SELECT * FROM t WHERE b = $1 AND a = $2", 2, 1)


def test_a_name_used_twice_is_one_parameter():
    # fetch_history's `($2::text IS NULL OR kind = $2)` -- the issue's motivator.
    assert named(
        "WHERE (:kind::text IS NULL OR kind = :kind) LIMIT :limit",
        {"kind": "voting_record", "limit": 5},
    ) == ("WHERE ($1::text IS NULL OR kind = $1) LIMIT $2", "voting_record", 5)


def test_type_casts_are_left_alone():
    assert named("SELECT :v::vector, col::jsonb", {"v": "[1]"}) == (
        "SELECT $1::vector, col::jsonb",
        "[1]",
    )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'a :b c' FROM t",
        "SELECT 'it''s :b' FROM t",
        'SELECT "odd:b" FROM t',
        "SELECT 1 -- see :b\nFROM t",
        "SELECT 1 /* :b\n:c */ FROM t",
    ],
)
def test_colons_inside_literals_identifiers_and_comments_are_left_alone(query):
    assert named(query, {}) == (query,)


@pytest.mark.parametrize(
    "query",
    [
        # Would come out as "SELECT $1, $1" -- both slots bound to :a.
        "SELECT $1, :a",
        "SELECT $$ :a $$",
        "SELECT $tag$ :a $tag$",
    ],
)
def test_a_dollar_outside_literals_raises(query):
    with pytest.raises(ValueError, match="unsupported SQL"):
        named(query, {"a": 1})


@pytest.mark.parametrize(
    "query",
    [
        # \' doesn't end an E'' literal, so the :a is still inside it.
        "SELECT E'it\\'s :a'",
        "SELECT e'x' , :a",
        "SELECT 'unterminated :a",
        'SELECT "unterminated :a',
    ],
)
def test_escape_strings_and_unterminated_quotes_raise(query):
    with pytest.raises(ValueError, match="unsupported SQL"):
        named(query, {"a": 1})


def test_typed_literals_ending_in_e_are_not_escape_strings():
    # date'...' is a plain typed literal; the E check needs a word boundary.
    assert named("SELECT date'2026-01-01', :a", {"a": 1}) == (
        "SELECT date'2026-01-01', $1",
        1,
    )


def test_a_dollar_inside_a_literal_is_fine():
    assert named("SELECT '$1', :a", {"a": 1}) == ("SELECT '$1', $1", 1)


def test_rewrite_is_deterministic():
    # asyncpg's prepared-statement cache keys on the exact query text.
    query = "WHERE a = :a AND b = :b"
    assert named(query, {"a": 1, "b": 2})[0] == named(query, {"b": 3, "a": 4})[0]


def test_missing_parameter_raises_key_error():
    with pytest.raises(KeyError, match="b"):
        named("WHERE a = :a AND b = :b", {"a": 1})


def test_unused_parameter_raises_value_error():
    with pytest.raises(ValueError, match="limt"):
        named("LIMIT :limit", {"limit": 5, "limt": 5})
