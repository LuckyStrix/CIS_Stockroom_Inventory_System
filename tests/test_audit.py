"""The audit guarantee.

The system's central promise is that nothing changes without being recorded.
These tests enforce it, including the reflective test at the bottom that
fails if someone adds a mutating service function and forgets its event.
"""

import inspect

import pytest

from stockroom import service
from stockroom.service import Actor, ConflictError

# Every public mutating function in service.py, and how to call it. Adding a
# mutation without adding it here fails test_every_mutation_is_covered.
MUTATIONS = {
    "create_item", "update_item", "archive_item", "restore_item",
    "assign_barcode", "create_person", "update_person", "get_or_create_person",
    "checkout", "return_loan",
}


def test_creating_an_item_is_logged(conn, actor):
    item = service.create_item(conn, actor=actor, name="Logged Item", quantity=3)
    event = service.list_events(conn, item_id=item.id)[0]
    assert event.action == "item.create"
    assert event.actor == "Test Operator <operator@rit.edu>"
    assert "Logged Item" in event.summary


def test_the_diff_records_before_and_after(conn, actor, item):
    service.update_item(conn, actor=actor, item_id=item.id,
                        name="Renamed", description="new words")
    event = service.list_events(conn, item_id=item.id, limit=1)[0]
    assert event.changes["name"] == {"from": "SanDisk 64GB SD Card", "to": "Renamed"}
    assert event.changes["description"]["to"] == "new words"


def test_checkout_and_return_are_both_logged(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    service.return_loan(conn, actor=actor, loan_id=loan.id)
    actions = [e.action for e in service.list_events(conn, item_id=item.id)]
    assert actions[:2] == ["loan.return", "loan.checkout"]


def test_partial_return_has_its_own_action(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=5)
    service.return_loan(conn, actor=actor, loan_id=loan.id, quantity=2)
    event = service.list_events(conn, item_id=item.id, limit=1)[0]
    assert event.action == "loan.partial_return"
    assert event.changes["still_out"] == {"from": 5, "to": 3}


def test_history_is_scoped_to_its_item(conn, actor, person):
    first = service.create_item(conn, actor=actor, name="First")
    second = service.create_item(conn, actor=actor, name="Second")
    service.checkout(conn, actor=actor, item_id=first.id, person_id=person.id)
    assert len(service.list_events(conn, item_id=first.id)) == 2
    assert len(service.list_events(conn, item_id=second.id)) == 1


def test_person_history_follows_the_person(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=1)
    events = service.list_events(conn, person_id=person.id)
    assert {e.action for e in events} == {"person.create", "loan.checkout"}


def test_a_rejected_change_writes_nothing(conn, actor, item, person):
    """A failed operation must leave neither a change nor a log entry."""
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=8)
    before = service.count_events(conn)
    with pytest.raises(ConflictError):
        service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=5)
    assert service.count_events(conn) == before
    assert service.get_item(conn, item.id).available == 2


def test_a_failed_update_rolls_back_completely(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=6)
    before = service.count_events(conn)
    with pytest.raises(ConflictError):
        # The rename is valid; the quantity is not. Neither may land.
        service.update_item(conn, actor=actor, item_id=item.id, name="Renamed", quantity=2)
    assert service.count_events(conn) == before
    assert service.get_item(conn, item.id).name == "SanDisk 64GB SD Card"


def test_events_are_never_modified(conn, actor, item):
    """The log is append-only: editing an item adds rows, never rewrites them."""
    first = service.list_events(conn, item_id=item.id)[0]
    service.update_item(conn, actor=actor, item_id=item.id, name="Changed")
    unchanged = [e for e in service.list_events(conn, item_id=item.id) if e.id == first.id][0]
    assert unchanged.summary == first.summary
    assert unchanged.at == first.at


def test_the_actor_is_recorded_per_change(conn, item):
    alice = Actor("Alice", "alice@rit.edu")
    bob = Actor("Bob", "bob@rit.edu")
    service.update_item(conn, actor=alice, item_id=item.id, shelf="A")
    service.update_item(conn, actor=bob, item_id=item.id, shelf="B")
    recent = service.list_events(conn, item_id=item.id, limit=2)
    assert [e.actor for e in recent] == ["Bob <bob@rit.edu>", "Alice <alice@rit.edu>"]


def test_system_actor_for_unattended_changes(conn):
    item = service.create_item(conn, actor=service.SYSTEM, name="Imported")
    assert service.list_events(conn, item_id=item.id)[0].actor == "system"


def test_every_mutation_is_covered(conn, actor, item, person):
    """Call every mutating function and assert each writes an event.

    This is the guard on the rule in service.py's docstring. If a new
    mutating function appears without a log_event call, this fails.
    """
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    spare = service.create_item(conn, actor=actor, name="Spare", generate_barcode=False)

    calls = {
        "create_item": lambda: service.create_item(conn, actor=actor, name="Fresh"),
        "update_item": lambda: service.update_item(conn, actor=actor, item_id=item.id,
                                                   description="edited"),
        "assign_barcode": lambda: service.assign_barcode(conn, actor=actor, item_id=spare.id),
        "create_person": lambda: service.create_person(conn, actor=actor, name="Zed",
                                                       email="zed@rit.edu"),
        "update_person": lambda: service.update_person(conn, actor=actor,
                                                       person_id=person.id, notes="vip"),
        "get_or_create_person": lambda: service.get_or_create_person(
            conn, actor=actor, name="Fresh Face", email="fresh@rit.edu"),
        "checkout": lambda: service.checkout(conn, actor=actor, item_id=item.id,
                                             person_id=person.id, quantity=1),
        "return_loan": lambda: service.return_loan(conn, actor=actor, loan_id=loan.id),
        "archive_item": lambda: service.archive_item(conn, actor=actor, item_id=spare.id),
        "restore_item": lambda: service.restore_item(conn, actor=actor, item_id=spare.id),
    }
    assert set(calls) == MUTATIONS, "MUTATIONS and the call table disagree"

    for name, call in calls.items():
        before = service.count_events(conn)
        call()
        assert service.count_events(conn) > before, f"{name} wrote no audit event"


def test_no_mutating_function_is_missing_from_the_table():
    """Catch a new public mutation that nobody added to MUTATIONS.

    Heuristic but effective: by this codebase's convention a function that
    changes something takes ``actor: Actor``. Read functions may also take an
    ``actor`` -- list_events uses it as a filter -- so the *annotation* is
    what distinguishes them, not the name.
    """
    found = set()
    for name, obj in vars(service).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != service.__name__:
            continue
        if name == "log_event":
            continue
        parameter = inspect.signature(obj).parameters.get("actor")
        if parameter is not None and "Actor" in str(parameter.annotation):
            found.add(name)
    missing = found - MUTATIONS
    assert not missing, (
        f"These service functions take an actor but are not audit-tested: {missing}. "
        "Add them to MUTATIONS and to the call table above."
    )
