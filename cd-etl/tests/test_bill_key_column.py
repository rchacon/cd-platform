import pytest

from cd.etl import bills_common
from conftest import random_number

# 119th Congress is seeded by migration 0001, so these don't need their
# own congresses row. pg_conn lives in conftest.py.
CONGRESS = 119


@pytest.fixture
def cleanup_bills(pg_conn):
    numbers = []
    yield numbers
    with pg_conn.cursor() as cursor:
        for bill_number in numbers:
            cursor.execute(
                "DELETE FROM bills WHERE congress = %s AND bill_number = %s",
                (CONGRESS, bill_number),
            )
    pg_conn.commit()


def _insert_bill(pg_conn, bill_type: str, bill_number: int) -> None:
    with pg_conn.cursor() as cursor:
        cursor.execute(
            bills_common.BILLS_UPSERT_SQL,
            (CONGRESS, bill_type, bill_number, "Test Bill Title", "Health", None, "hash-bill", None),
        )
    pg_conn.commit()


def _bill_key(pg_conn, bill_type: str, bill_number: int) -> str:
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT bill_key FROM bills WHERE congress = %s AND bill_type = %s AND bill_number = %s",
            (CONGRESS, bill_type, bill_number),
        )
        return cursor.fetchone()[0]


@pytest.mark.parametrize(
    "bill_type, expected_slug",
    [("HR", "hr"), ("S", "s"), ("SJRES", "sjres"), ("HCONRES", "hconres")],
)
def test_bill_key_is_congress_lowertype_number(pg_conn, cleanup_bills, bill_type, expected_slug):
    bill_number = random_number(20000, 29000)
    cleanup_bills.append(bill_number)

    _insert_bill(pg_conn, bill_type, bill_number)

    assert _bill_key(pg_conn, bill_type, bill_number) == f"{CONGRESS}-{expected_slug}-{bill_number}"


def test_bill_key_is_generated_not_writable(pg_conn, cleanup_bills):
    bill_number = random_number(20000, 29000)
    cleanup_bills.append(bill_number)
    _insert_bill(pg_conn, "HR", bill_number)

    with pg_conn.cursor() as cursor, pytest.raises(Exception):
        cursor.execute(
            "UPDATE bills SET bill_key = %s WHERE congress = %s AND bill_type = 'HR' AND bill_number = %s",
            ("119-hr-9999", CONGRESS, bill_number),
        )
    pg_conn.rollback()


def test_bill_key_is_not_null(pg_conn):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid = 'bills'::regclass AND attname = 'bill_key'"
        )
        row = cursor.fetchone()

    assert row is not None, "bills.bill_key should exist"
    assert row[0] is True, "bill_key should be NOT NULL (backstop for a missing CASE arm)"


def test_bill_key_has_a_unique_constraint(pg_conn):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT contype FROM pg_constraint "
            "WHERE conrelid = 'bills'::regclass AND conname = 'bills_unique_bill_key'"
        )
        row = cursor.fetchone()

    assert row is not None, "migration 0006 should add the bills_unique_bill_key constraint"
    assert row[0] == "u", "bills_unique_bill_key should be a UNIQUE constraint"
