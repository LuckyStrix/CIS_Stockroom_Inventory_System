"""The counter: baskets, bulk operations and kits.

The claim worth testing is atomicity. A basket of five items has to be one
decision, not five: if the last line cannot go out, none of the first four
should have either, or the record disagrees with what walked out of the room.

The basket itself is tested through HTTP, because its whole design -- hidden
form fields, no JavaScript, no server-side draft -- only exists at that level.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from stockroom import accounts, db, kits, service
from stockroom.service import Actor, ConflictError, NotFound, ValidationError

SETUP = Actor("cli:test")
STAFF_PASSWORD = "glass onion tuesday lamp"


@pytest.fixture
def tripod(conn, actor):
    return service.create_item(conn, actor=actor, name="Manfrotto tripod",
                               quantity=3, unit="Unit C", shelf="Floor")


@pytest.fixture
def battery(conn, actor):
    return service.create_item(conn, actor=actor, name="LP-E6NH battery",
                               quantity=6, unit="Unit A", shelf="Drawer 2")


# ---------------------------------------------------------------------------
# checkout_many
# ---------------------------------------------------------------------------


def test_a_basket_goes_out_in_one_go(conn, actor, item, tripod, person):
    loans = service.checkout_many(
        conn, actor=actor, person_id=person.id,
        lines=[(item.id, 2), (tripod.id, 1)],
    )
    assert len(loans) == 2
    assert service.get_item(conn, item.id).available == 8
    assert service.get_item(conn, tripod.id).available == 2


def test_one_bad_line_rolls_the_whole_basket_back(conn, actor, item, tripod,
                                                  person):
    """The reason this is one transaction and not a loop of checkouts."""
    with pytest.raises(ConflictError):
        service.checkout_many(
            conn, actor=actor, person_id=person.id,
            lines=[(item.id, 2), (tripod.id, 99)],   # only 3 tripods exist
        )

    assert service.get_item(conn, item.id).available == 10, \
        "the good line must not have survived the bad one"
    assert service.list_loans(conn, open_only=True) == []


def test_a_failed_basket_writes_no_history_either(conn, actor, item, tripod,
                                                  person):
    before = service.count_events(conn)
    with pytest.raises(ConflictError):
        service.checkout_many(conn, actor=actor, person_id=person.id,
                              lines=[(item.id, 1), (tripod.id, 99)])
    assert service.count_events(conn) == before
    assert service.verify_audit_chain(conn).ok


def test_a_basket_writes_a_line_per_item_and_one_summary(conn, actor, item,
                                                         tripod, person):
    service.checkout_many(conn, actor=actor, person_id=person.id,
                          lines=[(item.id, 2), (tripod.id, 1)])

    actions = [e.action for e in service.list_events(conn)]
    assert actions.count("loan.checkout") == 2
    assert actions.count("loan.checkout_batch") == 1

    batch = next(e for e in service.list_events(conn)
                 if e.action == "loan.checkout_batch")
    assert "Manfrotto tripod" in batch.summary
    assert "SanDisk" in batch.summary


def test_a_new_borrower_is_created_once_for_the_whole_basket(conn, actor, item,
                                                             tripod):
    service.checkout_many(
        conn, actor=actor, lines=[(item.id, 1), (tripod.id, 1)],
        person_name="Wren Okafor", person_email="wren@rit.edu",
    )
    matches = [p for p in service.list_people(conn) if p.email == "wren@rit.edu"]
    assert len(matches) == 1


def test_an_empty_basket_is_refused(conn, actor, person):
    with pytest.raises(ValidationError, match="nothing in the basket"):
        service.checkout_many(conn, actor=actor, person_id=person.id, lines=[])


def test_holds_are_respected_by_a_basket(conn, actor, item, person):
    """Stage B and Stage C meeting: a broken unit is not basket-able."""
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                      quantity=9)
    with pytest.raises(ConflictError):
        service.checkout_many(conn, actor=actor, person_id=person.id,
                              lines=[(item.id, 3)])


# ---------------------------------------------------------------------------
# return_many
# ---------------------------------------------------------------------------


def test_everything_comes_back_in_one_go(conn, actor, item, tripod, person):
    loans = service.checkout_many(conn, actor=actor, person_id=person.id,
                                  lines=[(item.id, 2), (tripod.id, 1)])

    service.return_many(conn, actor=actor, loan_ids=[l.id for l in loans])

    assert service.list_loans(conn, open_only=True) == []
    assert service.get_item(conn, item.id).available == 10
    assert service.get_item(conn, tripod.id).available == 3


def test_a_bad_loan_id_rolls_the_whole_return_back(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=2)

    with pytest.raises(NotFound):
        service.return_many(conn, actor=actor, loan_ids=[loan.id, 9999])

    assert service.get_loan(conn, loan.id).is_open, \
        "the valid return must not have survived the invalid one"


def test_returning_nothing_is_refused(conn, actor):
    with pytest.raises(ValidationError, match="nothing to return"):
        service.return_many(conn, actor=actor, loan_ids=[])


def test_open_loans_for_a_person_and_item_come_back_oldest_first(conn, actor,
                                                                 item, person):
    first = service.checkout(conn, actor=actor, item_id=item.id,
                             person_id=person.id, quantity=1)
    second = service.checkout(conn, actor=actor, item_id=item.id,
                              person_id=person.id, quantity=1)
    found = service.open_loans_for_item_and_person(
        conn, item_id=item.id, person_id=person.id
    )
    assert [l.id for l in found] == [first.id, second.id]


# ---------------------------------------------------------------------------
# kits
# ---------------------------------------------------------------------------


@pytest.fixture
def portrait_kit(conn, actor, item, tripod, battery):
    kit = kits.create_kit(conn, actor=actor, name="Portrait kit 1",
                          description="Body, sticks and power")
    return kits.set_kit_contents(
        conn, actor=actor, kit_id=kit.id,
        lines=[(item.id, 2), (tripod.id, 1), (battery.id, 2)],
    )


def test_a_kit_expands_into_basket_lines(conn, portrait_kit, item, tripod,
                                         battery):
    assert sorted(kits.basket_lines(portrait_kit)) == sorted([
        (item.id, 2), (tripod.id, 1), (battery.id, 2),
    ])
    assert portrait_kit.total_units == 5


def test_a_kit_knows_when_it_cannot_go_out(conn, actor, portrait_kit, tripod,
                                           person):
    assert portrait_kit.is_complete

    service.checkout(conn, actor=actor, item_id=tripod.id,
                     person_id=person.id, quantity=3)

    after = kits.get_kit(conn, portrait_kit.id)
    assert not after.is_complete
    assert [l.item_name for l in after.missing] == ["Manfrotto tripod"]
    assert after.missing[0].shortfall == 1


def test_a_broken_unit_makes_a_kit_incomplete(conn, actor, portrait_kit, tripod):
    """Availability is one formula, so holds reach kits for free."""
    service.open_hold(conn, actor=actor, item_id=tripod.id, state="broken",
                      quantity=3)
    assert not kits.get_kit(conn, portrait_kit.id).is_complete


def test_setting_contents_replaces_rather_than_merges(conn, actor, portrait_kit,
                                                      item):
    updated = kits.set_kit_contents(conn, actor=actor, kit_id=portrait_kit.id,
                                    lines=[(item.id, 1)])
    assert len(updated.lines) == 1
    assert updated.lines[0].quantity == 1


def test_a_zero_quantity_removes_a_line(conn, actor, portrait_kit, item, tripod,
                                        battery):
    updated = kits.set_kit_contents(
        conn, actor=actor, kit_id=portrait_kit.id,
        lines=[(item.id, 2), (tripod.id, 0), (battery.id, 2)],
    )
    assert "Manfrotto tripod" not in [l.item_name for l in updated.lines]


def test_changing_a_kit_does_not_touch_loans_it_produced(conn, actor,
                                                         portrait_kit, person,
                                                         item):
    """A kit is a shortcut for filling a basket, not a record of anything."""
    loans = service.checkout_many(conn, actor=actor, person_id=person.id,
                                  lines=kits.basket_lines(portrait_kit))
    kits.set_kit_contents(conn, actor=actor, kit_id=portrait_kit.id,
                          lines=[(item.id, 1)])

    still_open = service.list_loans(conn, open_only=True)
    assert len(still_open) == len(loans) == 3


def test_kit_names_are_unique(conn, actor, portrait_kit):
    with pytest.raises(ConflictError, match="already a kit"):
        kits.create_kit(conn, actor=actor, name="portrait kit 1")


def test_an_archived_item_cannot_join_a_kit(conn, actor, item, tripod):
    kit = kits.create_kit(conn, actor=actor, name="Doomed")
    service.archive_item(conn, actor=actor, item_id=tripod.id)
    with pytest.raises(ConflictError, match="archived"):
        kits.set_kit_contents(conn, actor=actor, kit_id=kit.id,
                              lines=[(tripod.id, 1)])


def test_an_item_knows_which_kits_need_it(conn, portrait_kit, tripod):
    found = kits.kits_containing(conn, tripod.id)
    assert [k.name for k in found] == ["Portrait kit 1"]


def test_archived_kits_are_hidden_by_default(conn, actor, portrait_kit):
    kits.archive_kit(conn, actor=actor, kit_id=portrait_kit.id)
    assert kits.list_kits(conn) == []
    assert len(kits.list_kits(conn, include_archived=True)) == 1


# ---------------------------------------------------------------------------
# the basket, through real HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as test_client:
        accounts.register(
            db.connect(), first_name="Test", last_name="Operator",
            email="operator@rit.edu", password=STAFF_PASSWORD,
            role="staff", status="active", actor=SETUP,
        )
        token = re.search(r'name="_csrf" value="([^"]+)"',
                          test_client.get("/login").text).group(1)
        response = test_client.post(
            "/login",
            data={"email": "operator@rit.edu", "password": STAFF_PASSWORD,
                  "next": "/", "_csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield test_client


def csrf(client, path):
    match = re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text)
    return match.group(1) if match else ""


def counter_post(client, path, data):
    payload = dict(data)
    payload["_csrf"] = csrf(client, "/counter")
    return client.post(path, data=payload, follow_redirects=True)


@pytest.fixture
def stock(client):
    """Two items, created through the UI so the barcodes are real."""
    conn = db.connect()
    a = service.create_item(conn, actor=SETUP, name="Canon EOS R5", quantity=2,
                            unit="Unit A", shelf="Shelf 1")
    b = service.create_item(conn, actor=SETUP, name="Manfrotto tripod",
                            quantity=3, unit="Unit C", shelf="Floor")
    return a, b


def test_the_counter_page_renders(client):
    body = client.get("/counter").text
    assert "Scan a barcode" in body
    assert "Nothing scanned yet" in body


def test_scanning_builds_a_basket_across_requests(client, stock):
    """The basket has to survive each POST, carried in hidden fields."""
    camera, tripod = stock

    body = counter_post(client, "/counter/add", {"code": camera.barcode}).text
    assert "Canon EOS R5" in body
    assert f'name="item_id" value="{camera.id}"' in body

    # Second scan: the form posts back the first line, so both must appear.
    body = counter_post(client, "/counter/add", {
        "code": tripod.barcode,
        "item_id": [str(camera.id)], "quantity": ["1"],
    }).text
    assert "Canon EOS R5" in body and "Manfrotto tripod" in body


def test_scanning_the_same_item_twice_increments_one_line(client, stock):
    camera, _ = stock
    body = counter_post(client, "/counter/add", {
        "code": camera.barcode,
        "item_id": [str(camera.id)], "quantity": ["1"],
    }).text
    assert body.count(f'name="item_id" value="{camera.id}"') == 1
    assert 'value="2"' in body


def test_an_unknown_scan_says_so_and_keeps_the_basket(client, stock):
    camera, _ = stock
    body = counter_post(client, "/counter/add", {
        "code": "NOT-A-REAL-CODE-999",
        "item_id": [str(camera.id)], "quantity": ["1"],
    }).text
    assert "Nothing matched" in body
    assert "Canon EOS R5" in body, "a bad scan must not empty the basket"


def test_the_whole_basket_checks_out_to_one_person(client, stock):
    camera, tripod = stock
    response = counter_post(client, "/counter/checkout", {
        "item_id": [str(camera.id), str(tripod.id)],
        "quantity": ["1", "2"],
        "borrower": "wren@rit.edu", "borrower_name": "Wren Okafor",
        "due_at": "", "note": "senior project",
    })
    assert "3 unit(s) across 2 item(s)" in response.text

    conn = db.connect()
    assert service.get_item(conn, camera.id).available == 1
    assert service.get_item(conn, tripod.id).available == 1


def test_an_impossible_basket_is_refused_and_kept(client, stock):
    camera, tripod = stock
    body = counter_post(client, "/counter/checkout", {
        "item_id": [str(camera.id), str(tripod.id)],
        "quantity": ["1", "99"],
        "borrower": "wren@rit.edu", "borrower_name": "Wren Okafor",
    }).text

    assert "available" in body
    assert "Canon EOS R5" in body, "the basket survives so it can be corrected"
    conn = db.connect()
    assert service.get_item(conn, camera.id).available == 2, "nothing was written"


def test_checking_out_with_no_borrower_asks_for_one(client, stock):
    camera, _ = stock
    body = counter_post(client, "/counter/checkout", {
        "item_id": [str(camera.id)], "quantity": ["1"], "borrower": "",
    }).text
    assert "Who is taking these?" in body


def test_a_line_can_be_removed(client, stock):
    camera, tripod = stock
    body = counter_post(client, "/counter/remove", {
        "item_id": [str(camera.id), str(tripod.id)],
        "quantity": ["1", "1"],
        "drop": str(camera.id),
    }).text
    assert "Manfrotto tripod" in body
    assert "Canon EOS R5" not in body


def test_a_kit_can_be_dropped_into_the_basket(client, stock):
    camera, tripod = stock
    conn = db.connect()
    kit = kits.create_kit(conn, actor=SETUP, name="Portrait kit 1")
    kits.set_kit_contents(conn, actor=SETUP, kit_id=kit.id,
                          lines=[(camera.id, 1), (tripod.id, 2)])

    body = counter_post(client, "/counter/add", {"kit_id": str(kit.id)}).text
    assert "Canon EOS R5" in body and "Manfrotto tripod" in body
    assert "3 unit(s)" in body


def test_the_return_desk_lists_what_one_person_has(client, stock):
    camera, tripod = stock
    conn = db.connect()
    person = service.get_or_create_person(conn, actor=SETUP, name="Wren",
                                          email="wren@rit.edu")
    service.checkout_many(conn, actor=SETUP, person_id=person.id,
                          lines=[(camera.id, 1), (tripod.id, 1)])

    body = client.get(f"/counter/return?person_id={person.id}").text
    assert "Wren has 2 item(s) out" in body
    assert "Canon EOS R5" in body


def test_ticked_loans_all_come_back_together(client, stock):
    camera, tripod = stock
    conn = db.connect()
    person = service.get_or_create_person(conn, actor=SETUP, name="Wren",
                                          email="wren@rit.edu")
    loans = service.checkout_many(conn, actor=SETUP, person_id=person.id,
                                  lines=[(camera.id, 1), (tripod.id, 1)])

    payload = {
        "person_id": str(person.id),
        "loan_id": [str(l.id) for l in loans],
        "note": "all present",
        "_csrf": csrf(client, f"/counter/return?person_id={person.id}"),
    }
    response = client.post("/counter/return", data=payload, follow_redirects=True)
    assert "Returned 2 item(s)" in response.text
    assert service.list_loans(db.connect(), open_only=True) == []


def test_the_kit_pages_render(client, stock):
    camera, _ = stock
    conn = db.connect()
    kit = kits.create_kit(conn, actor=SETUP, name="Portrait kit 1")
    kits.set_kit_contents(conn, actor=SETUP, kit_id=kit.id,
                          lines=[(camera.id, 1)])

    assert "Portrait kit 1" in client.get("/kits").text
    body = client.get(f"/kits/{kit.id}").text
    assert "Canon EOS R5" in body
    assert "Save contents" in body
