"""Upgrading an existing database.

The Pi runs `stockroom init` on every boot, against a database that may have
been created by any earlier version. These tests build the database an
upgrading machine actually has -- from the checked-in copy of the version 2
schema in tests/fixtures/ -- and prove it arrives at the same shape as a
database created fresh today, with its data and its history intact.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stockroom import db, service
from stockroom.service import Actor

_V2_SCHEMA = Path(__file__).parent / "fixtures" / "schema_v2.sql"

SETUP = Actor("cli:test")


def build_v2_database(path: Path, *, events: int = 5) -> Path:
    """Create a populated database exactly as schema version 2 left it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SCHEMA.read_text())
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('barcode_counter', '7')")
    conn.execute(
        "INSERT INTO person (name, email, created_at, updated_at) "
        "VALUES ('Alice Nguyen', 'alice@rit.edu', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO item (barcode, name, quantity, unit, shelf, created_at,"
        " updated_at) VALUES ('CIS-000007', 'Canon EOS R5', 3, 'Unit A',"
        " 'Shelf 1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    # A loan from before loans could name a unit -- v4 adds loan.unit_id, and
    # a row that predates it is the case that matters on a real Pi. Closed, so
    # it does not change what the item has available and disturb the
    # assertions above.
    conn.execute(
        "INSERT INTO loan (item_id, person_id, quantity, checked_out_at,"
        " returned_at, checked_out_by, returned_by)"
        " VALUES (1, 1, 1, '2026-01-02T00:00:00Z', '2026-01-09T00:00:00Z',"
        " 'cli:setup', 'cli:setup')"
    )
    for n in range(events):
        conn.execute(
            "INSERT INTO event (at, actor, action, entity_type, entity_id,"
            " item_id, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-01-{n + 1:02d}T00:00:00Z", "cli:setup", "item.create",
             "item", 1, 1, f"a change made before the log had hashes ({n})"),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def upgraded(temp_env):
    """A version 2 database, carried forward by init_db."""
    path = build_v2_database(temp_env / "old" / "stockroom.db")
    return db.init_db(path)


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    return {r["name"]: r["type"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'item_fts%'"
        )
    }


# ---------------------------------------------------------------------------
# the upgrade itself
# ---------------------------------------------------------------------------


def test_an_old_database_reaches_the_current_schema_version(upgraded):
    assert db.get_meta(upgraded, "schema_version") == str(db.SCHEMA_VERSION)


def test_the_columns_added_since_version_2_are_present(upgraded):
    assert "tracked" in _columns(upgraded, "item")
    assert "merged_into_id" in _columns(upgraded, "person")
    assert {"prev_hash", "hash"} <= set(_columns(upgraded, "event"))
    assert "unit_id" in _columns(upgraded, "loan")


def test_the_unit_column_survives_referencing_a_table_that_did_not_exist_yet(
        upgraded):
    """loan.unit_id references `unit`, which v2 does not have.

    _ensure_columns runs its ALTER TABLE before schema.sql creates that table,
    so this is a forward reference at DDL time. SQLite resolves foreign keys
    when a row is written rather than when the column is declared, so it is
    legal -- but it is exactly the sort of thing that works until it does not,
    and the failure would only appear on a real Pi upgrading in place.
    """
    assert "unit" in _tables(upgraded)
    row = upgraded.execute(
        "SELECT COUNT(*) AS n FROM pragma_foreign_key_list('loan') "
        "WHERE \"table\" = 'unit'"
    ).fetchone()
    assert row["n"] == 1, "the foreign key did not survive the ALTER"
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_loans_from_before_the_upgrade_name_no_unit(upgraded):
    """Which body went out on a loan closed last year is not recoverable.

    NULL is the honest answer; inventing one would be worse than admitting it.
    """
    rows = upgraded.execute("SELECT unit_id FROM loan").fetchall()
    assert rows, "the fixture should have loans to check"
    assert all(r["unit_id"] is None for r in rows)


def test_a_migrated_database_matches_a_fresh_one(upgraded, tmp_path):
    """The two paths into the schema must not drift apart.

    Every new column has to be written twice -- once in schema.sql for new
    databases and once in db._ADDED_COLUMNS for existing ones -- and forgetting
    the second is invisible until a real Pi upgrades. This compares them.
    """
    fresh = db.init_db(tmp_path / "fresh" / "stockroom.db")

    assert _tables(upgraded) == _tables(fresh)
    for table in sorted(_tables(fresh)):
        assert _columns(upgraded, table) == _columns(fresh, table), (
            f"table {table!r} has a different shape after migrating than it "
            "does when created fresh"
        )


def test_existing_data_survives_the_upgrade(upgraded):
    assert db.get_meta(upgraded, "barcode_counter") == "7"
    item = service.get_item(upgraded, 1)
    assert item.name == "Canon EOS R5"
    assert item.quantity == 3
    assert item.available == 3
    assert service.count_events(upgraded) == 5


def test_items_from_before_the_upgrade_are_not_unit_tracked(upgraded):
    """`tracked` has to default to 0, or every legacy item claims unit rows."""
    row = upgraded.execute("SELECT tracked FROM item WHERE id = 1").fetchone()
    assert row["tracked"] == 0


def test_running_init_twice_changes_nothing(upgraded):
    """systemd runs `stockroom init` on every boot."""
    before = service.verify_audit_chain(upgraded)
    again = db.init_db(upgraded.execute("PRAGMA database_list").fetchone()["file"])
    after = service.verify_audit_chain(again)
    assert after.ok
    assert after.head == before.head
    assert db.get_meta(again, "schema_version") == str(db.SCHEMA_VERSION)


def test_a_newer_database_is_refused_rather_than_guessed_at(temp_env):
    path = build_v2_database(temp_env / "future" / "stockroom.db")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="schema version 99"):
        db.init_db(path)


# ---------------------------------------------------------------------------
# the audit chain backfill
# ---------------------------------------------------------------------------


def test_events_written_before_the_chain_existed_are_chained(upgraded):
    result = service.verify_audit_chain(upgraded)
    assert result.ok, result
    assert result.checked == 5
    assert result.head


def test_new_events_chain_onto_the_backfilled_ones(upgraded):
    service.create_item(upgraded, actor=SETUP, name="Bought after the upgrade")
    result = service.verify_audit_chain(upgraded)
    assert result.ok, result
    assert result.checked == 6


def test_the_backfill_does_not_run_again_on_a_later_start(upgraded):
    """Re-running the backfill would launder a tamper into a valid chain.

    It is gated on the schema version for exactly that reason, so the only
    time it runs is the single upgrade, when there is nothing to launder.
    """
    upgraded.execute("UPDATE event SET summary = 'quietly rewritten' WHERE id = 2")
    upgraded.commit()

    path = upgraded.execute("PRAGMA database_list").fetchone()["file"]
    reopened = db.init_db(path)

    assert not service.verify_audit_chain(reopened).ok
