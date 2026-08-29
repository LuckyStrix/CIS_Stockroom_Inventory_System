"""Checkout and return, including the partial-return split and concurrency."""

import sqlite3
import threading

import pytest

from stockroom import db, service
from stockroom.service import ConflictError, ValidationError


def test_checkout_reduces_availability(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=3)
    assert loan.quantity == 3
    assert service.get_item(conn, item.id).available == 7


def test_several_people_can_hold_the_same_item(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    service.checkout(conn, actor=actor, item_id=item.id,
                     person_name="Bob", person_email="bob@rit.edu", quantity=1)
    current = service.get_item(conn, item.id)
    assert current.available == 7
    assert current.open_loan_count == 2


def test_cannot_take_more_than_is_available(conn, actor, item, person):
    with pytest.raises(ConflictError, match="available"):
        service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=11)


def test_cannot_take_the_last_unit_twice(conn, actor, person):
    single = service.create_item(conn, actor=actor, name="Only one", quantity=1)
    service.checkout(conn, actor=actor, item_id=single.id, person_id=person.id)
    with pytest.raises(ConflictError):
        service.checkout(conn, actor=actor, item_id=single.id,
                         person_name="Bob", person_email="bob@rit.edu")


def test_quantity_must_be_positive(conn, actor, item, person):
    with pytest.raises(ValidationError):
        service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=0)


def test_checkout_creates_an_unknown_borrower(conn, actor, item):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_name="New Person", person_email="new@rit.edu")
    assert loan.person_email == "new@rit.edu"
    assert service.find_person_by_email(conn, "NEW@RIT.EDU").name == "New Person"


def test_full_return_restores_availability(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=4)
    service.return_loan(conn, actor=actor, loan_id=loan.id)
    current = service.get_item(conn, item.id)
    assert current.available == 10
    assert current.open_loan_count == 0


def test_partial_return_splits_the_loan(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=5)
    service.return_loan(conn, actor=actor, loan_id=loan.id, quantity=2)

    current = service.get_item(conn, item.id)
    assert current.available == 7   # 10 - 5 + 2
    assert current.open_loan_count == 1

    residual = service.list_loans(conn, item_id=item.id, open_only=True)[0]
    assert residual.quantity == 3
    assert residual.split_from_loan_id == loan.id
    # The original checkout time survives, so "how long have they had it?"
    # is still answerable after a partial return.
    assert residual.checked_out_at == loan.checked_out_at

    original = service.get_loan(conn, loan.id)
    assert not original.is_open
    assert original.quantity == 5   # never rewritten


def test_partial_returns_can_be_chained(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=6)
    for _ in range(3):
        current = service.list_loans(conn, item_id=item.id, open_only=True)[0]
        service.return_loan(conn, actor=actor, loan_id=current.id, quantity=2)
    assert service.get_item(conn, item.id).available == 10
    assert service.get_item(conn, item.id).open_loan_count == 0


def test_cannot_return_more_than_is_out(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    with pytest.raises(ConflictError, match="only 2"):
        service.return_loan(conn, actor=actor, loan_id=loan.id, quantity=3)


def test_cannot_return_a_loan_twice(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=1)
    service.return_loan(conn, actor=actor, loan_id=loan.id)
    with pytest.raises(ConflictError, match="already returned"):
        service.return_loan(conn, actor=actor, loan_id=loan.id)


def test_overdue_detection(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=1, due_at="2020-01-01T00:00:00Z")
    overdue = service.list_loans(conn, overdue_only=True)
    assert len(overdue) == 1
    assert overdue[0].is_overdue(db.utcnow())
    assert service.summary(conn)["overdue_count"] == 1


def test_a_loan_with_no_due_date_is_never_overdue(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=1)
    assert service.list_loans(conn, overdue_only=True) == []


def test_concurrent_checkouts_cannot_oversubscribe(temp_env, actor):
    """Two threads racing for the last two units must not both win.

    This is the reason db.transaction() uses BEGIN IMMEDIATE: without the
    write lock taken up front, both threads would read available == 2 before
    either wrote, and the item would end up over-lent.
    """
    setup = db.init_db()
    scarce = service.create_item(setup, actor=actor, name="Scarce", quantity=2)

    results: list[str] = []
    barrier = threading.Barrier(4)

    def grab(email: str) -> None:
        conn = db.connect()           # thread-local connection
        barrier.wait()
        try:
            service.checkout(conn, actor=actor, item_id=scarce.id,
                             person_name=email, person_email=email, quantity=1)
            results.append("ok")
        except (ConflictError, sqlite3.OperationalError):
            results.append("rejected")
        finally:
            db.close_all()

    threads = [threading.Thread(target=grab, args=(f"p{i}@rit.edu",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "a checkout thread deadlocked"

    assert results.count("ok") == 2, results
    assert service.get_item(setup, scarce.id).available == 0
