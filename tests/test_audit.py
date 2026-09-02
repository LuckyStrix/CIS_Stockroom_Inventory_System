"""The audit guarantee.

The system's central promise is that nothing changes without being recorded.
These tests enforce it, including the reflective test at the bottom that
fails if someone adds a mutating service function and forgets its event.
"""

import inspect

import pytest

from stockroom import accounts, kits, requests_service, service, stocktake
from stockroom.service import Actor, ConflictError

# Every public mutating function across the three service modules, and how to
# call it. Adding a mutation without adding it here fails
# test_no_mutating_function_is_missing_from_the_table.
MUTATIONS = {
    "create_item", "update_item", "archive_item", "restore_item",
    "assign_barcode", "create_person", "update_person", "get_or_create_person",
    "checkout", "return_loan",
    "create_unit", "update_unit", "retire_unit",
    "open_hold", "change_hold", "close_hold",
    "checkout_many", "return_many", "merge_people",
    "add_photo", "set_primary_photo", "remove_photo",
}

# accounts.py and requests_service.py follow the same rule as service.py. They
# are separate modules only because service.py owns inventory and was already
# long -- the audit guarantee is identical, so the guard covers all three.
AUDITED_MODULES = (service, accounts, requests_service, kits, stocktake)

ACCOUNT_MUTATIONS = {
    "register", "approve", "set_status", "set_role", "change_password",
    "login", "logout", "revoke_all_sessions", "sso_login",
}

KIT_MUTATIONS = {
    "create_kit", "update_kit", "set_kit_contents", "archive_kit", "restore_kit",
}

# record_scan is deliberately absent from the audit rule's usual shape: it
# takes an actor but writes no event, because a thousand-item count would bury
# the inventory history it exists to protect. Its scans live in
# stocktake_scan, and the session itself is audited at start and finish. This
# is the second documented exception, alongside the session heartbeat.
STOCKTAKE_MUTATIONS = {
    "start_stocktake", "record_scan", "finish_stocktake", "abandon_stocktake",
}

REQUEST_MUTATIONS = {
    "submit_borrow", "submit_new_item", "submit_open_hours",
    "approve", "decline", "cancel", "fulfil_borrow", "fulfil_new_item",
    "add_open_hours", "cancel_open_hours",
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


def _a_real_jpeg() -> bytes:
    """A tiny genuine JPEG. service.add_photo decodes what it is given."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 18), "grey").save(buffer, "JPEG")
    return buffer.getvalue()


def test_every_mutation_is_covered(conn, actor, item, person):
    """Call every mutating function and assert each writes an event.

    This is the guard on the rule in service.py's docstring. If a new
    mutating function appears without a log_event call, this fails.
    """
    loan = service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=2)
    spare = service.create_item(conn, actor=actor, name="Spare", generate_barcode=False)
    tracked = service.create_item(conn, actor=actor, name="Camera body", quantity=2,
                                  tracked=1, generate_barcode=False)
    unit = service.create_unit(conn, actor=actor, item_id=tracked.id, asset_tag="AT-1")
    hold = service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                             quantity=1, note="cracked case")
    first_photo = service.add_photo(conn, actor=actor, item_id=item.id,
                                    data=_a_real_jpeg())
    second_photo = service.add_photo(conn, actor=actor, item_id=item.id,
                                     data=_a_real_jpeg())

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
        "create_unit": lambda: service.create_unit(conn, actor=actor,
                                                   item_id=tracked.id, asset_tag="AT-2"),
        "update_unit": lambda: service.update_unit(conn, actor=actor, unit_id=unit.id,
                                                   serial="SN-99"),
        "retire_unit": lambda: service.retire_unit(conn, actor=actor, unit_id=unit.id),
        "open_hold": lambda: service.open_hold(conn, actor=actor, item_id=item.id,
                                               state="missing", quantity=1),
        "change_hold": lambda: service.change_hold(conn, actor=actor, hold_id=hold.id,
                                                   state="repair"),
        "close_hold": lambda: service.close_hold(conn, actor=actor, hold_id=hold.id,
                                                 resolution="fixed"),
        "checkout_many": lambda: service.checkout_many(
            conn, actor=actor, person_id=person.id,
            lines=[(item.id, 1), (tracked.id, 1)]),
        "add_photo": lambda: service.add_photo(
            conn, actor=actor, item_id=item.id, data=_a_real_jpeg()),
        "set_primary_photo": lambda: service.set_primary_photo(
            conn, actor=actor, photo_id=second_photo.id),
        "remove_photo": lambda: service.remove_photo(
            conn, actor=actor, photo_id=first_photo.id),
        "merge_people": lambda: service.merge_people(
            conn, actor=actor,
            keep_id=person.id,
            merge_id=service.create_person(
                conn, actor=actor, name="Alice Nguyen",
                email="an1234@rit.edu").id),
        "return_many": lambda: service.return_many(
            conn, actor=actor,
            loan_ids=[l.id for l in service.list_loans(
                conn, person_id=person.id, open_only=True)][:1]),
    }
    assert set(calls) == MUTATIONS, "MUTATIONS and the call table disagree"

    for name, call in calls.items():
        before = service.count_events(conn)
        call()
        assert service.count_events(conn) > before, f"{name} wrote no audit event"


def test_every_account_mutation_is_audited(conn, actor):
    """The same guarantee, in accounts.py."""
    admin = accounts.register(
        conn, first_name="Root", last_name="Admin", email="root@rit.edu",
        password="glass onion tuesday lamp", role="admin", status="active",
        actor=actor,
    )
    subject = accounts.register(
        conn, first_name="Alice", last_name="Nguyen", email="an1234@rit.edu",
        password="seventeen purple bicycles", actor=actor,
    )
    calls = {
        "register": lambda: accounts.register(
            conn, first_name="New", last_name="Person", email="np1111@rit.edu",
            password="Rochester-Fog-Kettle-9", actor=actor),
        "approve": lambda: accounts.approve(
            conn, actor=actor, account_id=subject.id, approved_by=admin),
        "set_role": lambda: accounts.set_role(
            conn, actor=actor, account_id=subject.id, role="staff"),
        "change_password": lambda: accounts.change_password(
            conn, actor=actor, account_id=subject.id,
            new_password="Kettle-Fog-Rochester-4"),
        "login": lambda: accounts.login(
            conn, email="an1234@rit.edu", password="Kettle-Fog-Rochester-4"),
        "logout": lambda: accounts.logout(
            conn, token=accounts.login(
                conn, email="an1234@rit.edu",
                password="Kettle-Fog-Rochester-4").token),
        "revoke_all_sessions": lambda: accounts.login(
            conn, email="an1234@rit.edu", password="Kettle-Fog-Rochester-4"
        ) and accounts.revoke_all_sessions(
            conn, actor=actor, account_id=subject.id),
        "set_status": lambda: accounts.set_status(
            conn, actor=actor, account_id=subject.id, status="disabled"),
        # A fresh address, so this cannot be perturbed by -- or perturb --
        # the set_status entry that disables `subject` at the end. The first
        # SSO sign-in provisions and signs in, which is two events; the loop
        # only asks that the count went up.
        "sso_login": lambda: accounts.sso_login(
            conn, actor=actor, sso_uid="zz9999", email="zz9999@rit.edu",
            first_name="Zoe", last_name="Zimmer", affiliation="Student"),
    }
    assert set(calls) == ACCOUNT_MUTATIONS, "the call table is out of date"

    for name, call in calls.items():
        before = service.count_events(conn)
        call()
        assert service.count_events(conn) > before, f"{name} wrote no audit event"


def test_every_request_mutation_is_audited(conn, actor, item):
    """The same guarantee, in requests_service.py."""
    admin = accounts.register(
        conn, first_name="Root", last_name="Admin", email="root@rit.edu",
        password="glass onion tuesday lamp", role="admin", status="active",
        actor=actor,
    )
    filed = requests_service.submit_borrow(
        conn, actor=actor, requester_id=admin.id, item_id=item.id, quantity=1)
    to_decline = requests_service.submit_new_item(
        conn, actor=actor, requester_id=admin.id, name="Declinable")
    to_cancel = requests_service.submit_new_item(
        conn, actor=actor, requester_id=admin.id, name="Cancellable")
    to_fulfil_item = requests_service.submit_new_item(
        conn, actor=actor, requester_id=admin.id, name="Fulfillable")
    requests_service.approve(
        conn, actor=actor, request_id=to_fulfil_item.id, decided_by_id=admin.id)
    slot = requests_service.add_open_hours(
        conn, actor=actor, window_start="2099-01-01T09:00:00Z",
        window_end="2099-01-01T11:00:00Z")

    calls = {
        "submit_borrow": lambda: requests_service.submit_borrow(
            conn, actor=actor, requester_id=admin.id, item_id=item.id, quantity=1),
        "submit_new_item": lambda: requests_service.submit_new_item(
            conn, actor=actor, requester_id=admin.id, name="Another"),
        "submit_open_hours": lambda: requests_service.submit_open_hours(
            conn, actor=actor, requester_id=admin.id,
            window_start="2099-02-01T09:00:00Z",
            window_end="2099-02-01T11:00:00Z", purpose="both"),
        "approve": lambda: requests_service.approve(
            conn, actor=actor, request_id=filed.id, decided_by_id=admin.id),
        "fulfil_borrow": lambda: requests_service.fulfil_borrow(
            conn, actor=actor, request_id=filed.id),
        "fulfil_new_item": lambda: requests_service.fulfil_new_item(
            conn, actor=actor, request_id=to_fulfil_item.id, item_id=item.id),
        "decline": lambda: requests_service.decline(
            conn, actor=actor, request_id=to_decline.id, decided_by_id=admin.id),
        "cancel": lambda: requests_service.cancel(
            conn, actor=actor, request_id=to_cancel.id, by_account_id=admin.id),
        "add_open_hours": lambda: requests_service.add_open_hours(
            conn, actor=actor, window_start="2099-03-01T09:00:00Z",
            window_end="2099-03-01T11:00:00Z"),
        "cancel_open_hours": lambda: requests_service.cancel_open_hours(
            conn, actor=actor, slot_id=slot.id),
    }
    assert set(calls) == REQUEST_MUTATIONS, "the call table is out of date"

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
    expected = {
        service.__name__: MUTATIONS,
        accounts.__name__: ACCOUNT_MUTATIONS,
        requests_service.__name__: REQUEST_MUTATIONS,
        kits.__name__: KIT_MUTATIONS,
        stocktake.__name__: STOCKTAKE_MUTATIONS,
    }
    problems = []
    for module in AUDITED_MODULES:
        found = set()
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != module.__name__:
                continue
            if name in {"log_event", "check_password_strength"}:
                continue
            parameter = inspect.signature(obj).parameters.get("actor")
            if parameter is not None and "Actor" in str(parameter.annotation):
                found.add(name)
        missing = found - expected[module.__name__]
        if missing:
            problems.append(f"{module.__name__}: {sorted(missing)}")

    assert not problems, (
        "These functions take an actor but are not audit-tested: "
        + "; ".join(problems)
        + ". Add each to the matching set and call table in this file."
    )


# ---------------------------------------------------------------------------
# the hash chain
#
# The audit log is the point of the system, and it is a plain SQLite table
# that anyone with shell access can edit. The chain does not stop that -- it
# makes it detectable. These tests are what prove the detection works.
# ---------------------------------------------------------------------------


def test_ordinary_use_produces_a_chain_that_verifies(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=2)
    service.update_item(conn, actor=actor, item_id=item.id, shelf="Shelf 9")

    result = service.verify_audit_chain(conn)
    assert result.ok, result
    assert result.checked == service.count_events(conn)
    assert result.head == service.audit_head(conn)


def test_an_empty_log_verifies(conn):
    result = service.verify_audit_chain(conn)
    assert result.ok
    assert result.checked == 0
    assert result.head == ""


def test_the_head_moves_with_every_change(conn, actor, item):
    first = service.audit_head(conn)
    service.update_item(conn, actor=actor, item_id=item.id, description="edited")
    assert service.audit_head(conn) != first


def test_editing_an_event_is_detected_and_the_row_is_named(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id)
    target = conn.execute(
        "SELECT id FROM event WHERE action = 'loan.checkout'"
    ).fetchone()["id"]

    # The realistic tamper: quietly change what the record says happened.
    conn.execute(
        "UPDATE event SET summary = 'nothing to see here' WHERE id = ?", (target,)
    )
    conn.commit()

    result = service.verify_audit_chain(conn)
    assert not result.ok
    assert result.broken_at == target
    assert "edited" in result.detail


def test_deleting_an_event_is_detected(conn, actor, item):
    service.update_item(conn, actor=actor, item_id=item.id, shelf="Shelf 4")
    service.update_item(conn, actor=actor, item_id=item.id, shelf="Shelf 5")
    victim = conn.execute("SELECT id FROM event ORDER BY id").fetchall()[1]["id"]

    conn.execute("DELETE FROM event WHERE id = ?", (victim,))
    conn.commit()

    result = service.verify_audit_chain(conn)
    assert not result.ok
    assert "removed" in result.detail or "inserted" in result.detail


def test_a_forged_event_appended_to_the_log_is_detected(conn, actor, item):
    """A row inserted directly, with no idea what the chain state was."""
    service.update_item(conn, actor=actor, item_id=item.id, shelf="Shelf 4")
    conn.execute(
        "INSERT INTO event (at, actor, action, entity_type, entity_id, item_id,"
        " summary, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-08-31T00:00:00Z", "someone", "loan.return", "loan", 99, item.id,
         "returned, honest", "made up", "made up"),
    )
    conn.commit()

    assert not service.verify_audit_chain(conn).ok


def test_changing_an_actor_is_detected(conn, actor, item):
    """Who did it is part of the digest, not just what was done."""
    service.archive_item(conn, actor=actor, item_id=item.id)
    conn.execute("UPDATE event SET actor = 'somebody else' WHERE id = 1")
    conn.commit()

    result = service.verify_audit_chain(conn)
    assert not result.ok
    assert result.broken_at == 1


def test_reordering_events_is_detected(conn, actor, item):
    """Swapping two rows' contents leaves both hashes wrong."""
    service.update_item(conn, actor=actor, item_id=item.id, shelf="A")
    service.update_item(conn, actor=actor, item_id=item.id, shelf="B")
    rows = conn.execute(
        "SELECT id, summary FROM event ORDER BY id DESC LIMIT 2"
    ).fetchall()
    conn.execute("UPDATE event SET summary = ? WHERE id = ?",
                 (rows[1]["summary"], rows[0]["id"]))
    conn.execute("UPDATE event SET summary = ? WHERE id = ?",
                 (rows[0]["summary"], rows[1]["id"]))
    conn.commit()

    assert not service.verify_audit_chain(conn).ok


def test_every_kit_mutation_is_audited(conn, actor, item):
    """The same guarantee, in kits.py."""
    spare = service.create_item(conn, actor=actor, name="Tripod", quantity=2)
    kit = kits.create_kit(conn, actor=actor, name="Portrait kit 1")
    doomed = kits.create_kit(conn, actor=actor, name="Old kit")
    kits.archive_kit(conn, actor=actor, kit_id=doomed.id)

    calls = {
        "create_kit": lambda: kits.create_kit(conn, actor=actor, name="Video kit"),
        "update_kit": lambda: kits.update_kit(conn, actor=actor, kit_id=kit.id,
                                              description="for headshots"),
        "set_kit_contents": lambda: kits.set_kit_contents(
            conn, actor=actor, kit_id=kit.id,
            lines=[(item.id, 2), (spare.id, 1)]),
        "archive_kit": lambda: kits.archive_kit(conn, actor=actor, kit_id=kit.id),
        "restore_kit": lambda: kits.restore_kit(conn, actor=actor, kit_id=doomed.id),
    }
    assert set(calls) == KIT_MUTATIONS, "KIT_MUTATIONS and the call table disagree"

    for name, call in calls.items():
        before = service.count_events(conn)
        call()
        assert service.count_events(conn) > before, f"{name} wrote no audit event"


def test_every_stocktake_mutation_is_audited(conn, actor, item):
    """The same guarantee in stocktake.py, with one documented exception."""
    session = stocktake.start_stocktake(conn, actor=actor, note="monthly")

    audited = {
        "start_stocktake": lambda: stocktake.start_stocktake(
            conn, actor=actor, scope_unit="Unit B"),
        "finish_stocktake": lambda: stocktake.finish_stocktake(
            conn, actor=actor, stocktake_id=session.id),
        "abandon_stocktake": lambda: stocktake.abandon_stocktake(
            conn, actor=actor,
            stocktake_id=stocktake.open_stocktake(conn).id),
    }
    assert set(audited) | {"record_scan"} == STOCKTAKE_MUTATIONS, \
        "STOCKTAKE_MUTATIONS and the call table disagree"

    # finish first, then start another, then abandon it -- one open at a time.
    for name in ("finish_stocktake", "start_stocktake", "abandon_stocktake"):
        before = service.count_events(conn)
        audited[name]()
        assert service.count_events(conn) > before, f"{name} wrote no audit event"


def test_a_single_scan_deliberately_writes_no_event(conn, actor, item):
    """The documented exception, pinned so it cannot drift into an accident.

    A stocktake of a thousand items would put a thousand rows in the history
    and bury the inventory record the log exists to protect -- the same
    reasoning as the session heartbeat. The scans are in stocktake_scan, and
    the session is audited at both ends.
    """
    session = stocktake.start_stocktake(conn, actor=actor)
    before = service.count_events(conn)

    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          item_id=item.id, quantity=3)

    assert service.count_events(conn) == before
    assert stocktake.scan_counts(conn, session.id) == {item.id: 3}
    assert service.verify_audit_chain(conn).ok
