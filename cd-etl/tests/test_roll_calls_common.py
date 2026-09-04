from cd.etl import roll_calls_common
from cd.etl.dags import house_votes_etl


def test_vote_cast_map_covers_both_chambers_wording():
    # House: Aye/No (Recorded Vote) or Yea/Nay (Yea-and-Nay).
    # Senate: Yea/Nay/Present/Not Voting.
    m = roll_calls_common.VOTE_CAST_MAP
    assert m["yea"] == m["aye"] == "YEA"
    assert m["nay"] == m["no"] == "NAY"
    assert m["present"] == "PRESENT"
    assert m["not voting"] == "NOT_VOTING"


def test_house_votes_etl_re_exports_the_shared_symbols():
    # house_votes_etl imports these from roll_calls_common now; the
    # re-export keeps `house_votes_etl.X` working (existing tests and the
    # upsert-SQL suite reference them that way).
    assert house_votes_etl.get_or_sync_bill is roll_calls_common.get_or_sync_bill
    assert house_votes_etl.ROLL_CALLS_UPSERT_SQL is roll_calls_common.ROLL_CALLS_UPSERT_SQL
    assert (
        house_votes_etl.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL
        is roll_calls_common.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL
    )
    assert house_votes_etl.VOTE_CAST_MAP is roll_calls_common.VOTE_CAST_MAP
