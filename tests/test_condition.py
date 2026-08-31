"""Broken things, missing things, and which one it was.

Two gaps this closes. The system used to have no way to say a camera came
back damaged -- it stayed "available" and the public page went on advertising
it -- and no way to say *which* of four identical bodies anything happened to.

The invariant under all of it is one line in the item_status view:

    available = quantity - (units on loan) - (units held out of service)

`quantity` never moves. A written-off unit is a permanent hold, not a quantity
edit, so the stockroom can still say "we bought ten and two are unaccounted
for" -- which is the sentence that gets a replacement budgeted.
"""

from __future__ import annotations

import pytest

from stockroom import db, service
from stockroom.service import ConflictError, ValidationError


@pytest.fixture
def camera(conn, actor):
    """Four individually tracked camera bodies."""
    return service.create_item(
        conn, actor=actor, name="Canon EOS R5", quantity=4,
        unit="Unit A", shelf="Shelf 1", tracked=True,
    )


@pytest.fixture
def bodies(conn, actor, camera):
    return [
        service.create_unit(conn, actor=actor, item_id=camera.id,
                            asset_tag=f"CIS-U-{n}", serial=f"SN{n:04d}")
        for n in range(1, 5)
    ]


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def test_a_hold_takes_units_off_the_shelf(conn, actor, item):
    assert service.get_item(conn, item.id).available == 10

    service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                      quantity=3, note="water damage")

    after = service.get_item(conn, item.id)
    assert after.available == 7
    assert after.held_qty == 3
    assert after.quantity == 10, "a broken unit is still a unit the stockroom owns"


def test_closing_a_hold_puts_them_back(conn, actor, item):
    hold = service.open_hold(conn, actor=actor, item_id=item.id,
                             state="repair", quantity=2)
    assert service.get_item(conn, item.id).available == 8

    service.close_hold(conn, actor=actor, hold_id=hold.id, resolution="repaired")

    back = service.get_item(conn, item.id)
    assert back.available == 10
    assert back.held_qty == 0


def test_written_off_units_stay_in_the_owned_count(conn, actor, item):
    """The decision the plan settled: 'gone' does not rewrite history."""
    service.open_hold(conn, actor=actor, item_id=item.id, state="gone",
                      quantity=2, note="never came back from the field trip")

    after = service.get_item(conn, item.id)
    assert after.quantity == 10
    assert after.available == 8
    assert after.unaccounted_qty == 2


def test_broken_units_are_held_but_not_unaccounted_for(conn, actor, item):
    """Somebody knows where a broken lens is. That is the distinction."""
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken", quantity=1)
    service.open_hold(conn, actor=actor, item_id=item.id, state="missing", quantity=2)

    after = service.get_item(conn, item.id)
    assert after.held_qty == 3
    assert after.unaccounted_qty == 2


def test_loans_and_holds_both_reduce_availability(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=3)
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken", quantity=2)

    after = service.get_item(conn, item.id)
    assert (after.quantity, after.out_qty, after.held_qty, after.available) == (
        10, 3, 2, 5,
    )


def test_you_cannot_hold_more_than_is_on_the_shelf(conn, actor, item, person):
    """Units already lent out cannot also be in the repair pile."""
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=8)

    with pytest.raises(ConflictError, match="on the shelf"):
        service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                          quantity=3)


def test_a_held_unit_cannot_be_checked_out(conn, actor, item, person):
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken", quantity=9)

    with pytest.raises(ConflictError, match="available"):
        service.checkout(conn, actor=actor, item_id=item.id,
                         person_id=person.id, quantity=2)


def test_quantity_cannot_drop_below_what_is_loaned_and_held(conn, actor, item,
                                                            person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=3)
    service.open_hold(conn, actor=actor, item_id=item.id, state="repair", quantity=4)

    with pytest.raises(ConflictError, match="out of service"):
        service.update_item(conn, actor=actor, item_id=item.id, quantity=5)

    # 7 is exactly what is spoken for, so it is allowed.
    service.update_item(conn, actor=actor, item_id=item.id, quantity=7)
    assert service.get_item(conn, item.id).available == 0


def test_availability_never_goes_negative(conn, actor, item, person):
    """The guard above is the only thing standing between here and nonsense."""
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id,
                     quantity=5)
    service.open_hold(conn, actor=actor, item_id=item.id, state="gone", quantity=5)
    assert service.get_item(conn, item.id).available == 0

    with pytest.raises(ConflictError):
        service.update_item(conn, actor=actor, item_id=item.id, quantity=2)


def test_the_status_label_says_why_nothing_is_available(conn, actor, item, person):
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken", quantity=10)
    assert service.get_item(conn, item.id).status_label == "Unavailable"

    other = service.create_item(conn, actor=actor, name="Tripod", quantity=1)
    service.checkout(conn, actor=actor, item_id=other.id, person_id=person.id)
    assert service.get_item(conn, other.id).status_label == "All out"


# ---------------------------------------------------------------------------
# state transitions
# ---------------------------------------------------------------------------


def test_a_hold_moves_through_its_states(conn, actor, item):
    hold = service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                             quantity=1, note="shutter jammed")

    hold = service.change_hold(conn, actor=actor, hold_id=hold.id, state="repair",
                               note="sent to the depot")
    assert hold.state == "repair"
    assert service.get_item(conn, item.id).available == 9, \
        "moving between states does not change how many are on the shelf"

    hold = service.change_hold(conn, actor=actor, hold_id=hold.id, state="gone",
                               note="beyond economic repair")
    assert hold.state == "gone"
    assert service.get_item(conn, item.id).unaccounted_qty == 1


def test_an_unknown_state_is_refused(conn, actor, item):
    with pytest.raises(ValidationError, match="not a condition"):
        service.open_hold(conn, actor=actor, item_id=item.id, state="a bit dented")


def test_a_closed_hold_cannot_be_changed_or_closed_again(conn, actor, item):
    hold = service.open_hold(conn, actor=actor, item_id=item.id, state="broken")
    service.close_hold(conn, actor=actor, hold_id=hold.id)

    with pytest.raises(ConflictError):
        service.change_hold(conn, actor=actor, hold_id=hold.id, state="repair")
    with pytest.raises(ConflictError):
        service.close_hold(conn, actor=actor, hold_id=hold.id)


# ---------------------------------------------------------------------------
# returning something damaged
# ---------------------------------------------------------------------------


def test_returning_damaged_records_who_had_it(conn, actor, item, person):
    """The whole reason the hold links to the loan."""
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=2)

    service.return_loan(conn, actor=actor, loan_id=loan.id,
                        condition="broken", note="one card will not mount")

    holds = service.list_holds(conn, item_id=item.id)
    assert len(holds) == 1
    assert holds[0].loan_id == loan.id
    assert holds[0].borrower_name == "Alice Nguyen"
    assert holds[0].borrower_email == "alice@rit.edu"


def test_returning_damaged_reduces_availability(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=2)
    service.return_loan(conn, actor=actor, loan_id=loan.id, condition="broken")

    after = service.get_item(conn, item.id)
    assert after.out_qty == 0, "the loan is closed"
    assert after.held_qty == 2, "but they did not go back on the shelf"
    assert after.available == 8


def test_only_part_of_a_return_can_be_damaged(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=4)

    service.return_loan(conn, actor=actor, loan_id=loan.id,
                        condition="broken", condition_quantity=1)

    after = service.get_item(conn, item.id)
    assert after.held_qty == 1
    assert after.available == 9


def test_you_cannot_damage_more_than_came_back(conn, actor, item, person):
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=2)

    with pytest.raises(ConflictError, match="only 2"):
        service.return_loan(conn, actor=actor, loan_id=loan.id,
                            condition="broken", condition_quantity=3)


def test_a_damaged_partial_return_still_splits_the_loan(conn, actor, item, person):
    """Two mechanisms meeting: the residual loan and the hold."""
    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=5)

    service.return_loan(conn, actor=actor, loan_id=loan.id, quantity=2,
                        condition="broken")

    after = service.get_item(conn, item.id)
    assert after.out_qty == 3, "three are still with the borrower"
    assert after.held_qty == 2, "the two that came back are not lendable"
    assert after.available == 5


# ---------------------------------------------------------------------------
# individual units
# ---------------------------------------------------------------------------


def test_units_start_available(conn, bodies):
    assert [u.state for u in bodies] == ["ok"] * 4
    assert all(u.is_available for u in bodies)


def test_a_unit_can_be_marked_broken_by_name(conn, actor, camera, bodies):
    service.open_hold(conn, actor=actor, item_id=camera.id, state="broken",
                      unit_id=bodies[1].id, note="bent lens mount")

    states = {u.asset_tag: u.state for u in service.list_units(conn, item_id=camera.id)}
    assert states == {"CIS-U-1": "ok", "CIS-U-2": "broken",
                      "CIS-U-3": "ok", "CIS-U-4": "ok"}
    assert service.get_item(conn, camera.id).available == 3


def test_a_unit_hold_covers_exactly_one_thing(conn, actor, camera, bodies):
    """A named physical object cannot be three of anything."""
    hold = service.open_hold(conn, actor=actor, item_id=camera.id, state="broken",
                             unit_id=bodies[0].id, quantity=3)
    assert hold.quantity == 1


def test_a_unit_cannot_be_broken_twice(conn, actor, camera, bodies):
    service.open_hold(conn, actor=actor, item_id=camera.id, state="broken",
                      unit_id=bodies[0].id)
    with pytest.raises(ConflictError, match="already recorded as"):
        service.open_hold(conn, actor=actor, item_id=camera.id, state="missing",
                          unit_id=bodies[0].id)


def test_a_unit_cannot_be_held_against_the_wrong_item(conn, actor, camera,
                                                      bodies, item):
    with pytest.raises(ValidationError, match="does not belong"):
        service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                          unit_id=bodies[0].id)


def test_asset_tags_are_unique_across_the_stockroom(conn, actor, camera, bodies):
    with pytest.raises(ConflictError, match="already belongs"):
        service.create_unit(conn, actor=actor, item_id=camera.id,
                            asset_tag="CIS-U-1")


def test_a_unit_can_be_looked_up_by_its_asset_tag(conn, bodies):
    found = service.get_unit_by_asset_tag(conn, "CIS-U-3")
    assert found is not None
    assert found.serial == "SN0003"
    assert service.get_unit_by_asset_tag(conn, "nothing-like-this") is None


def test_a_retired_unit_keeps_its_history(conn, actor, camera, bodies):
    service.retire_unit(conn, actor=actor, unit_id=bodies[3].id, reason="sold")

    assert len(service.list_units(conn, item_id=camera.id)) == 3
    still_there = service.list_units(conn, item_id=camera.id, include_retired=True)
    assert len(still_there) == 4
    assert service.get_unit(conn, bodies[3].id).is_retired


def test_a_unit_label_prefers_the_asset_tag(conn, actor, camera):
    plain = service.create_unit(conn, actor=actor, item_id=camera.id,
                                serial="SN-ONLY")
    assert plain.label == "SN-ONLY"
    tagged = service.create_unit(conn, actor=actor, item_id=camera.id,
                                 asset_tag="TAG", serial="SN-BOTH")
    assert tagged.label == "TAG"


# ---------------------------------------------------------------------------
# what the public sees
# ---------------------------------------------------------------------------


def test_the_public_page_stops_advertising_a_broken_camera(conn, actor, camera,
                                                           bodies):
    """The correctness bug this stage exists to fix."""
    from stockroom.publish.render import build_payload

    service.open_hold(conn, actor=actor, item_id=camera.id, state="broken",
                      unit_id=bodies[0].id, note="bent lens mount")

    published = next(
        i for i in build_payload(conn)["items"] if i["name"] == "Canon EOS R5"
    )
    assert published["available"] == 3
    assert published["quantity"] == 4


def test_the_public_page_does_not_say_what_is_wrong_with_it(conn, actor, camera,
                                                            bodies):
    """Availability counts, never the stockroom's internal notes."""
    from stockroom.publish.render import render_json

    service.open_hold(conn, actor=actor, item_id=camera.id, state="broken",
                      unit_id=bodies[0].id,
                      note="dropped by a student on the roof")

    feed = render_json(conn)
    assert "dropped by a student" not in feed
    assert "CIS-U-1" not in feed


# ---------------------------------------------------------------------------
# the audit trail
# ---------------------------------------------------------------------------


def test_every_condition_change_is_in_the_history(conn, actor, item):
    hold = service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                             note="cracked")
    service.change_hold(conn, actor=actor, hold_id=hold.id, state="repair")
    service.close_hold(conn, actor=actor, hold_id=hold.id, resolution="fixed")

    actions = [e.action for e in service.list_events(conn, item_id=item.id)]
    assert "item.hold_open" in actions
    assert "item.hold_change" in actions
    assert "item.hold_close" in actions
    assert service.verify_audit_chain(conn).ok


def test_the_history_says_what_broke_and_why(conn, actor, item):
    service.open_hold(conn, actor=actor, item_id=item.id, state="broken",
                      quantity=2, note="left in the rain")
    latest = service.list_events(conn, item_id=item.id)[0]
    assert "broken" in latest.summary
    assert "left in the rain" in latest.summary
