from datetime import date

import pytest

from cd.etl import senate_votes_source as src

# Trimmed to the elements the parser reads; structure matches the real
# senate.gov feed (verified live against the 119th Congress, session 1).

_MENU_XML = b"""<?xml version="1.0" encoding="UTF-8"?><vote_summary>
  <congress>119</congress>
  <session>1</session>
  <votes>
    <vote>
      <vote_number>00648</vote_number>
      <issue>S. 1071</issue>
      <question>On the Motion</question>
      <result>Agreed to</result>
    </vote>
    <vote>
      <vote_number>00659</vote_number>
      <issue>PN373</issue>
    </vote>
    <vote>
      <vote_number>00655</vote_number>
      <en_bloc>
        <matter><issue>PN416-9</issue></matter>
        <matter><issue>PN141-12</issue></matter>
      </en_bloc>
    </vote>
    <vote>
      <vote_number>bogus</vote_number>
      <issue>S. 9</issue>
    </vote>
  </votes>
</vote_summary>"""

_DETAIL_XML = b"""<?xml version="1.0" encoding="UTF-8"?><roll_call_vote>
  <congress>119</congress>
  <session>1</session>
  <vote_number>648</vote_number>
  <vote_date>December 17, 2025,  11:39 AM</vote_date>
  <question>On the Motion</question>
  <vote_result>Motion Agreed to</vote_result>
  <document>
    <document_congress>119</document_congress>
    <document_type>S.</document_type>
    <document_number>1071</document_number>
    <document_name>S. 1071</document_name>
  </document>
  <amendment>
    <amendment_number/>
    <amendment_to_document_number/>
  </amendment>
  <members>
    <member>
      <last_name>Alsobrooks</last_name>
      <vote_cast>Yea</vote_cast>
      <lis_member_id>S428</lis_member_id>
    </member>
    <member>
      <last_name>Barrasso</last_name>
      <vote_cast>Not Voting</vote_cast>
      <lis_member_id>S317</lis_member_id>
    </member>
  </members>
</roll_call_vote>"""

_AMENDMENT_DETAIL_XML = b"""<?xml version="1.0" encoding="UTF-8"?><roll_call_vote>
  <congress>119</congress>
  <session>1</session>
  <vote_number>400</vote_number>
  <vote_date>June 30, 2025</vote_date>
  <question>On the Amendment</question>
  <vote_result>Amendment Rejected</vote_result>
  <document>
    <document_type>S.Amdt.</document_type>
    <document_number/>
  </document>
  <amendment>
    <amendment_number>2</amendment_number>
    <amendment_to_document_number>H.R. 4</amendment_to_document_number>
  </amendment>
  <members>
    <member><vote_cast>Nay</vote_cast><lis_member_id>S428</lis_member_id></member>
  </members>
</roll_call_vote>"""


# --- parse_vote_menu ---


def test_parse_vote_menu_reads_number_and_issue():
    items = src.parse_vote_menu(_MENU_XML)
    by_number = {i.vote_number: i.issue for i in items}
    assert by_number[648] == "S. 1071"
    assert by_number[659] == "PN373"


def test_parse_vote_menu_gives_en_bloc_vote_a_null_issue():
    items = {i.vote_number: i for i in src.parse_vote_menu(_MENU_XML)}
    assert items[655].issue is None


def test_parse_vote_menu_skips_row_with_unparseable_vote_number():
    numbers = [i.vote_number for i in src.parse_vote_menu(_MENU_XML)]
    assert numbers == [648, 659, 655]  # the "bogus" row is dropped


# --- parse_vote_detail ---


def test_parse_vote_detail_extracts_the_scalar_fields():
    detail = src.parse_vote_detail(_DETAIL_XML)
    assert (detail.congress, detail.session, detail.vote_number) == (119, 1, 648)
    assert detail.question == "On the Motion"
    assert detail.result == "Motion Agreed to"
    assert detail.vote_date == date(2025, 12, 17)


def test_parse_vote_detail_reads_the_document_block():
    doc = src.parse_vote_detail(_DETAIL_XML).document
    assert (doc.document_type, doc.document_number, doc.document_congress) == ("S.", "1071", 119)


def test_parse_vote_detail_reads_member_casts_by_lis_id():
    casts = {m.lis_member_id: m.vote_cast for m in src.parse_vote_detail(_DETAIL_XML).member_votes}
    assert casts == {"S428": "Yea", "S317": "Not Voting"}


def test_parse_vote_detail_handles_a_date_with_no_time():
    detail = src.parse_vote_detail(_AMENDMENT_DETAIL_XML)
    assert detail.vote_date == date(2025, 6, 30)


def test_parse_vote_detail_leaves_blank_document_fields_none():
    detail = src.parse_vote_detail(_AMENDMENT_DETAIL_XML)
    assert detail.document.document_type == "S.Amdt."
    assert detail.document.document_number is None
    assert detail.amendment.amendment_to_document_number == "H.R. 4"


# --- bill_type_for ---


@pytest.mark.parametrize(
    "display, expected",
    [
        ("S.", "S"),
        ("H.R.", "HR"),
        ("S.J.Res.", "SJRES"),
        ("H.J.Res.", "HJRES"),
        ("S.Con.Res.", "SCONRES"),
        ("H.Con.Res.", "HCONRES"),
        ("S.Res.", "SRES"),
        ("H.Res.", "HRES"),
        ("  s.j.res.  ", "SJRES"),  # whitespace + case tolerant
    ],
)
def test_bill_type_for_maps_known_display_forms(display, expected):
    assert src.bill_type_for(display) == expected


@pytest.mark.parametrize("display", ["PN", "Treaty Doc.", "S.Amdt.", "H.Amdt.", "", None, "???"])
def test_bill_type_for_returns_none_for_non_bill_types(display):
    assert src.bill_type_for(display) is None


# --- parse_bill_reference ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ("H.R. 4", ("HR", 4)),
        ("S.J.Res. 82", ("SJRES", 82)),
        ("S. 1071", ("S", 1071)),
        ("  H.Con.Res. 14 ", ("HCONRES", 14)),
    ],
)
def test_parse_bill_reference_parses_combined_display_strings(text, expected):
    assert src.parse_bill_reference(text) == expected


@pytest.mark.parametrize("text", ["PN 373", "S.Amdt. 2", "not a bill", "", None, "H.R."])
def test_parse_bill_reference_returns_none_for_non_bills(text):
    assert src.parse_bill_reference(text) is None
