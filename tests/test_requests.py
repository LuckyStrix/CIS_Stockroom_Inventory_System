"""The three request workflows, and confirmed open hours."""

import pytest

from stockroom import accounts, requests_service as rq, service
from stockroom.service import Actor, ConflictError, ValidationError

SETUP = Actor("cli:test")
STRONG = "glass onion tuesday lamp"
OTHER = "seventeen purple bicycles"


@pytest.fixture
def admin(conn):
    return accounts.register(
        conn, first_name="Carter", last_name="Laubach", email="carter@rit.edu",
        password=STRONG, role="admin", status="active", actor=SETUP,
    )


@pytest.fixture
def alice(conn, admin):
    account = accounts.register(
        conn, first_name="Alice", last_name="Nguyen", email="an1234@rit.edu",
        password=OTHER, actor=SETUP,
    )
    return accounts.approve(conn, actor=admin.as_actor(), account_id=account.id,
                            approved_by=admin)


@pytest.fixture
def camera(conn, admin):
    return service.create_item(conn, actor=admin.as_actor(), name="Canon EOS R5",
                               quantity=3, unit="Unit A", shelf="Shelf 1")


# ---------------------------------------------------------------------------
# borrow
# ---------------------------------------------------------------------------


def test_a_borrow_request_starts_pending(conn, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=2, note="senior project")
    assert made.status == "pending"
    assert made.quantity == 2
    assert made.requester_name == "Alice Nguyen"


def test_a_request_does_not_reserve_stock(conn, alice, camera):
    """Availability must reflect the shelf, not intentions about it."""
    rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                     item_id=camera.id, quantity=2)
    assert service.get_item(conn, camera.id).available == 3


def test_cannot_request_more_than_the_stockroom_owns(conn, alice, camera):
    with pytest.raises(ConflictError, match="only owns"):
        rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                         item_id=camera.id, quantity=9)


def test_cannot_request_an_archived_item(conn, admin, alice, camera):
    service.archive_item(conn, actor=admin.as_actor(), item_id=camera.id)
    with pytest.raises(ConflictError, match="archived"):
        rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                         item_id=camera.id, quantity=1)


def test_approval_alone_moves_no_equipment(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=2)
    approved = rq.approve(conn, actor=admin.as_actor(), request_id=made.id,
                          decided_by_id=admin.id)
    assert approved.status == "approved"
    assert approved.loan_id is None
    assert service.get_item(conn, camera.id).available == 3


def test_fulfilment_creates_the_loan(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=2)
    rq.approve(conn, actor=admin.as_actor(), request_id=made.id, decided_by_id=admin.id)
    done = rq.fulfil_borrow(conn, actor=admin.as_actor(), request_id=made.id)

    assert done.status == "fulfilled"
    assert done.loan_id is not None
    assert service.get_item(conn, camera.id).available == 1

    loan = service.get_loan(conn, done.loan_id)
    assert loan.person_email == "an1234@rit.edu"
    assert loan.quantity == 2


def test_fulfilment_obeys_the_availability_invariant(conn, admin, alice, camera):
    """Requests are not a side door around the inventory rules."""
    first = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                             item_id=camera.id, quantity=3)
    second = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                              item_id=camera.id, quantity=3)
    for made in (first, second):
        rq.approve(conn, actor=admin.as_actor(), request_id=made.id,
                   decided_by_id=admin.id)
    rq.fulfil_borrow(conn, actor=admin.as_actor(), request_id=first.id)
    with pytest.raises(ConflictError, match="available"):
        rq.fulfil_borrow(conn, actor=admin.as_actor(), request_id=second.id)


def test_cannot_fulfil_before_approval(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    with pytest.raises(ConflictError, match="approve it"):
        rq.fulfil_borrow(conn, actor=admin.as_actor(), request_id=made.id)


# ---------------------------------------------------------------------------
# new item
# ---------------------------------------------------------------------------


def test_a_new_item_request_round_trips(conn, admin, alice):
    made = rq.submit_new_item(conn, actor=alice.as_actor(), requester_id=alice.id,
                              name="Second tripod", description="always out",
                              quantity=1, vendor="B&H")
    assert made.status == "pending" and made.title == "Second tripod"

    rq.approve(conn, actor=admin.as_actor(), request_id=made.id, decided_by_id=admin.id)
    created = service.create_item(conn, actor=admin.as_actor(),
                                  name="Second tripod", quantity=1)
    done = rq.fulfil_new_item(conn, actor=admin.as_actor(), request_id=made.id,
                              item_id=created.id)
    assert done.status == "fulfilled"
    assert done.created_item_id == created.id


def test_a_new_item_request_needs_a_name(conn, alice):
    with pytest.raises(ValidationError):
        rq.submit_new_item(conn, actor=alice.as_actor(), requester_id=alice.id,
                           name="   ")


# ---------------------------------------------------------------------------
# open hours
# ---------------------------------------------------------------------------


def test_approving_open_hours_publishes_a_slot(conn, admin, alice):
    made = rq.submit_open_hours(
        conn, actor=alice.as_actor(), requester_id=alice.id,
        window_start="2099-09-03T14:00:00Z", window_end="2099-09-03T16:00:00Z",
        purpose="return", note="bringing the R5 back",
    )
    done = rq.approve(conn, actor=admin.as_actor(), request_id=made.id,
                      decided_by_id=admin.id)
    # Nothing further is needed, so it completes on approval.
    assert done.status == "fulfilled"

    slots = rq.list_open_hours(conn)
    assert len(slots) == 1
    assert slots[0].window_start == "2099-09-03T14:00:00Z"
    assert slots[0].request_id == made.id


def test_a_backwards_window_is_refused(conn, alice):
    with pytest.raises(ValidationError, match="after the start"):
        rq.submit_open_hours(conn, actor=alice.as_actor(), requester_id=alice.id,
                             window_start="2099-09-03T16:00:00Z",
                             window_end="2099-09-03T14:00:00Z", purpose="both")


def test_an_unknown_purpose_is_refused(conn, alice):
    with pytest.raises(ValidationError):
        rq.submit_open_hours(conn, actor=alice.as_actor(), requester_id=alice.id,
                             window_start="2099-09-03T14:00:00Z",
                             window_end="2099-09-03T16:00:00Z", purpose="loiter")


def test_past_slots_are_not_listed_as_upcoming(conn, admin):
    rq.add_open_hours(conn, actor=admin.as_actor(),
                      window_start="2000-01-01T09:00:00Z",
                      window_end="2000-01-01T11:00:00Z")
    rq.add_open_hours(conn, actor=admin.as_actor(),
                      window_start="2099-01-01T09:00:00Z",
                      window_end="2099-01-01T11:00:00Z")
    assert len(rq.list_open_hours(conn, upcoming_only=True)) == 1
    assert len(rq.list_open_hours(conn, upcoming_only=False)) == 2


def test_a_cancelled_slot_disappears(conn, admin):
    slot = rq.add_open_hours(conn, actor=admin.as_actor(),
                             window_start="2099-01-01T09:00:00Z",
                             window_end="2099-01-01T11:00:00Z")
    rq.cancel_open_hours(conn, actor=admin.as_actor(), slot_id=slot.id)
    assert rq.list_open_hours(conn, upcoming_only=False) == []


# ---------------------------------------------------------------------------
# the shared lifecycle
# ---------------------------------------------------------------------------


def test_declining_records_the_reason(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    done = rq.decline(conn, actor=admin.as_actor(), request_id=made.id,
                      decided_by_id=admin.id, note="already booked that week")
    assert done.status == "declined"
    assert done.decision_note == "already booked that week"
    assert done.decided_by_name == "Carter Laubach"


def test_a_requester_can_withdraw(conn, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    done = rq.cancel(conn, actor=alice.as_actor(), request_id=made.id,
                     by_account_id=alice.id)
    assert done.status == "cancelled"


def test_a_settled_request_cannot_be_decided_again(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    rq.decline(conn, actor=admin.as_actor(), request_id=made.id, decided_by_id=admin.id)
    with pytest.raises(ConflictError, match="already declined"):
        rq.approve(conn, actor=admin.as_actor(), request_id=made.id,
                   decided_by_id=admin.id)


def test_the_pending_count_drives_the_staff_badge(conn, admin, alice, camera):
    assert rq.count_pending(conn) == 0
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    assert rq.count_pending(conn) == 1
    rq.approve(conn, actor=admin.as_actor(), request_id=made.id, decided_by_id=admin.id)
    assert rq.count_pending(conn) == 0


def test_every_request_transition_is_audited(conn, admin, alice, camera):
    made = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                            item_id=camera.id, quantity=1)
    rq.approve(conn, actor=admin.as_actor(), request_id=made.id, decided_by_id=admin.id)
    rq.fulfil_borrow(conn, actor=admin.as_actor(), request_id=made.id)

    actions = [e.action for e in service.list_events(conn)]
    for expected in ("request.create", "request.approve", "request.fulfil",
                     "loan.checkout"):
        assert expected in actions


def test_requests_are_listed_pending_first(conn, admin, alice, camera):
    settled = rq.submit_borrow(conn, actor=alice.as_actor(), requester_id=alice.id,
                               item_id=camera.id, quantity=1)
    rq.decline(conn, actor=admin.as_actor(), request_id=settled.id,
               decided_by_id=admin.id)
    waiting = rq.submit_new_item(conn, actor=alice.as_actor(),
                                 requester_id=alice.id, name="Something else")
    assert rq.list_requests(conn)[0].id == waiting.id
