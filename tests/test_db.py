"""Database mechanics: schema, transactions, backups."""

import sqlite3

import pytest

from stockroom import db, service


def test_init_is_idempotent(temp_env):
    first = db.init_db()
    before = db.get_meta(first, "created_at")
    second = db.init_db()
    assert db.get_meta(second, "created_at") == before


def test_wal_mode_is_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO loan (item_id, person_id, quantity, checked_out_at, "
            "checked_out_by) VALUES (999, 999, 1, '2026-01-01T00:00:00Z', 'x')"
        )


def test_check_constraints_hold(conn, actor, item, person):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE item SET quantity = -1 WHERE id = ?", (item.id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO loan (item_id, person_id, quantity, checked_out_at, "
            "checked_out_by) VALUES (?, ?, 0, '2026-01-01T00:00:00Z', 'x')",
            (item.id, person.id),
        )


def test_email_is_unique_case_insensitively(conn, actor, person):
    from stockroom.service import ConflictError

    with pytest.raises(ConflictError):
        service.create_person(conn, actor=actor, name="Other", email="ALICE@RIT.EDU")


def test_a_failed_transaction_rolls_everything_back(conn, actor):
    before = service.count_events(conn)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            service.create_item(conn, actor=actor, name="Doomed")
            raise RuntimeError("boom")
    assert service.count_events(conn) == before
    assert service.list_items(conn) == []


def test_nested_transactions_commit_once(conn, actor):
    """Service functions call one another; the outer block owns the commit."""
    with db.transaction(conn):
        service.create_item(conn, actor=actor, name="A")
        service.create_item(conn, actor=actor, name="B")
    assert len(service.list_items(conn)) == 2


def test_a_nested_failure_rolls_back_the_whole_outer_block(conn, actor):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            service.create_item(conn, actor=actor, name="First")
            with db.transaction(conn):
                service.create_item(conn, actor=actor, name="Second")
            raise RuntimeError("boom")
    assert service.list_items(conn) == []


def test_timestamps_sort_chronologically(conn):
    """ISO-8601 UTC text sorts correctly, which the overdue query relies on."""
    assert "2026-01-01T00:00:00Z" < "2026-01-02T00:00:00Z"
    assert "2026-09-30T23:59:59Z" < "2026-10-01T00:00:00Z"
    assert len(db.utcnow()) == 20 and db.utcnow().endswith("Z")


def test_backup_produces_a_readable_copy(conn, actor, item, tmp_path):
    target = tmp_path / "snapshot.db"
    db.backup(target)
    assert target.exists()

    restored = sqlite3.connect(str(target))
    restored.row_factory = sqlite3.Row
    names = [r["name"] for r in restored.execute("SELECT name FROM item")]
    assert names == [item.name]
    restored.close()


def test_backup_captures_the_audit_log(conn, actor, item, person, tmp_path):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    target = tmp_path / "snapshot.db"
    db.backup(target)

    restored = sqlite3.connect(str(target))
    count = restored.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    assert count == service.count_events(conn)
    restored.close()


def test_item_status_view_stays_consistent(conn, actor, item, person):
    """Availability is derived, so it can never disagree with the loans."""
    for quantity in (1, 2, 3):
        service.checkout(conn, actor=actor, item_id=item.id,
                         person_id=person.id, quantity=quantity)
    row = conn.execute(
        "SELECT quantity, out_qty, available FROM item_status WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["out_qty"] == 6
    assert row["available"] == row["quantity"] - row["out_qty"] == 4
