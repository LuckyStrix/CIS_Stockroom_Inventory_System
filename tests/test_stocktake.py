"""Counting the shelves, merging duplicate people, and chasing stale requests.

The reconciliation maths is the part worth pinning down: "expected on the
shelf" has to mean exactly what item_status.available means, or a stocktake
invents discrepancies that are really just loans and repairs it forgot about.
"""

from __future__ import annotations

import pytest

from stockroom import requests_service as rq
from stockroom import accounts, config, db, service, stocktake
from stockroom.service import Actor, ConflictError, NotFound, ValidationError

SETUP = Actor("cli:test")
STRONG = "glass onion tuesday lamp"


@pytest.fixture
def shelf(conn, actor):
    """Three items in one storage unit, plus one somewhere else."""
    return {
        "cards": service.create_item(conn, actor=actor, name="SD cards",
                                     quantity=10, unit="Unit B", shelf="3"),
        "tripod": service.create_item(conn, actor=actor, name="Tripod",
                                      quantity=2, unit="Unit B", shelf="Floor"),
        "reader": service.create_item(conn, actor=actor, name="Card reader",
                                      quantity=4, unit="Unit B", shelf="3"),
        "camera": service.create_item(conn, actor=actor, name="Camera",
                                      quantity=1, unit="Unit A", shelf="1"),
    }


def scan_all(conn, actor, session, pairs):
    for item, count in pairs:
        for _ in range(count):
            stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                                  item_id=item.id)


# ---------------------------------------------------------------------------
# the count
# ---------------------------------------------------------------------------


def test_a_clean_count_finds_nothing_wrong(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 10), (shelf["tripod"], 2), (shelf["reader"], 4)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert result.is_clean, [d.item_name for d in result.problems]
    assert result.counted_items == 3


def test_a_shortfall_is_found(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 8), (shelf["tripod"], 2), (shelf["reader"], 4)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert [d.item_name for d in result.short] == ["SD cards"]
    assert result.short[0].difference == -2
    assert result.short[0].label == "2 missing"


def test_more_on_the_shelf_than_expected_is_also_a_discrepancy(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 12), (shelf["tripod"], 2), (shelf["reader"], 4)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert [d.item_name for d in result.over] == ["SD cards"]
    assert result.over[0].label == "2 more than expected"


def test_an_item_nobody_scanned_is_reported_separately(conn, actor, shelf):
    """Distinguished from a shortfall: the likeliest cause is a skipped shelf."""
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session, [(shelf["cards"], 10), (shelf["tripod"], 2)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert [d.item_name for d in result.unscanned] == ["Card reader"]
    assert result.short == []


def test_loaned_units_are_not_expected_on_the_shelf(conn, actor, shelf, person):
    """The whole point of deriving 'expected' from item_status.available."""
    service.checkout(conn, actor=actor, item_id=shelf["cards"].id,
                     person_id=person.id, quantity=4)

    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 6), (shelf["tripod"], 2), (shelf["reader"], 4)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert result.is_clean, "six on the shelf is correct when four are out"


def test_units_out_of_service_are_not_expected_either(conn, actor, shelf):
    """Stage B and Stage D meeting through the same derived formula."""
    service.open_hold(conn, actor=actor, item_id=shelf["reader"].id,
                      state="repair", quantity=2)

    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 10), (shelf["tripod"], 2), (shelf["reader"], 2)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert result.is_clean


def test_a_scope_limits_what_is_expected(conn, actor, shelf):
    """Counting Unit B must not report every item in Unit A as missing."""
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 10), (shelf["tripod"], 2), (shelf["reader"], 4)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert "Camera" not in [d.item_name for d in result.problems]


def test_something_found_outside_its_own_unit_is_flagged(conn, actor, shelf):
    """A misfiled item is its own kind of lost."""
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 10), (shelf["tripod"], 2), (shelf["reader"], 4),
              (shelf["camera"], 1)])

    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert "Camera" in [d.item_name for d in result.over]


def test_scanning_the_same_thing_twice_counts_two(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor)
    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          item_id=shelf["cards"].id)
    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          item_id=shelf["cards"].id)
    assert stocktake.scan_counts(conn, session.id)[shelf["cards"].id] == 2


def test_a_barcode_can_be_scanned(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor)
    _, message = stocktake.record_scan(conn, actor=actor,
                                       stocktake_id=session.id,
                                       code=shelf["cards"].barcode)
    assert "SD cards" in message


def test_an_asset_tag_counts_its_item(conn, actor):
    """Scanning the individual camera body counts one of that item."""
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=2, tracked=True)
    service.create_unit(conn, actor=actor, item_id=camera.id, asset_tag="CIS-U-9")

    session = stocktake.start_stocktake(conn, actor=actor)
    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          code="CIS-U-9")
    assert stocktake.scan_counts(conn, session.id)[camera.id] == 1


def test_scanning_one_asset_tag_twice_still_counts_one(conn, actor):
    """A physical object cannot be on the shelf twice.

    The two kinds of scan count in opposite directions: six scans of a card
    box are six boxes, but six scans of one camera's asset tag are one camera
    passed under the scanner six times. Adding them reported a body the
    stockroom did not own, and double-scanning while walking a room is the
    likeliest operator error there is.
    """
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=1, tracked=True)
    service.create_unit(conn, actor=actor, item_id=camera.id, asset_tag="CIS-U-9")

    session = stocktake.start_stocktake(conn, actor=actor)
    for _ in range(3):
        stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                              code="CIS-U-9")

    assert stocktake.scan_counts(conn, session.id)[camera.id] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM stocktake_scan WHERE stocktake_id = ?",
        (session.id,),
    ).fetchone()[0] == 1, "a re-scanned unit must not open a second row"
    # And therefore no phantom surplus in the report.
    assert stocktake.reconcile(conn, session.id).over == []


def test_two_units_of_one_item_are_two(conn, actor):
    """The dedup is per object, not per item -- two bodies really are two."""
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=2, tracked=True)
    for tag in ("CIS-U-1", "CIS-U-2"):
        service.create_unit(conn, actor=actor, item_id=camera.id, asset_tag=tag)

    session = stocktake.start_stocktake(conn, actor=actor)
    for tag in ("CIS-U-1", "CIS-U-2", "CIS-U-1"):
        stocktake.record_scan(conn, actor=actor, stocktake_id=session.id, code=tag)

    assert stocktake.scan_counts(conn, session.id)[camera.id] == 2


def test_scanning_a_countable_barcode_still_accumulates(conn, actor, shelf):
    """The other half of the rule, which the unit fix must not disturb."""
    session = stocktake.start_stocktake(conn, actor=actor)
    for _ in range(6):
        stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                              code=shelf["cards"].barcode)
    assert stocktake.scan_counts(conn, session.id)[shelf["cards"].id] == 6


def test_scanning_something_unknown_says_it_may_never_have_been_entered(conn,
                                                                        actor):
    session = stocktake.start_stocktake(conn, actor=actor)
    with pytest.raises(NotFound, match="never entered"):
        stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                              code="MYSTERY-BOX-1")


def test_only_one_stocktake_can_be_open(conn, actor):
    stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit A")
    with pytest.raises(ConflictError, match="already in progress"):
        stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")


def test_a_finished_stocktake_frees_the_slot(conn, actor):
    first = stocktake.start_stocktake(conn, actor=actor)
    stocktake.finish_stocktake(conn, actor=actor, stocktake_id=first.id)
    second = stocktake.start_stocktake(conn, actor=actor)
    assert second.id != first.id


def test_a_closed_stocktake_takes_no_more_scans(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor)
    stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    with pytest.raises(ConflictError, match="closed"):
        stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                              item_id=shelf["cards"].id)


def test_abandoning_keeps_the_scans(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor)
    stocktake.record_scan(conn, actor=actor, stocktake_id=session.id,
                          item_id=shelf["cards"].id, quantity=5)
    stocktake.abandon_stocktake(conn, actor=actor, stocktake_id=session.id,
                                reason="ran out of time")
    assert stocktake.scan_counts(conn, session.id) == {shelf["cards"].id: 5}


def test_a_report_shows_what_it_found_not_what_is_true_now(conn, actor, shelf,
                                                           person):
    """Reopening an old count must not re-derive against today's shelves.

    It used to: `expected` came from item_status at read time, so a count that
    finished clean in March grew discrepancies in April as things were lent
    out, and nobody reading the report could tell which numbers were findings
    and which were just this morning's loans. A finished stocktake is an
    observation of one day, so it is recorded rather than recomputed.
    """
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 10), (shelf["tripod"], 2), (shelf["reader"], 4)])
    result = stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)
    assert result.is_clean

    service.checkout(conn, actor=actor, item_id=shelf["cards"].id,
                     person_id=person.id, quantity=3)

    later = stocktake.reconcile(conn, session.id)
    assert later.is_clean, "a finished report changed after the shelves moved"
    assert [(d.item_name, d.expected, d.counted) for d in later.matched] == \
           [(d.item_name, d.expected, d.counted) for d in result.matched]


def test_an_open_count_still_reflects_the_shelves_as_they_are(conn, actor,
                                                              shelf, person):
    """The freeze applies to a finished count, not to one in progress.

    While somebody is walking the room the whole value of the progress view is
    that it is live.
    """
    service.checkout(conn, actor=actor, item_id=shelf["cards"].id,
                     person_id=person.id, quantity=3)
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session,
             [(shelf["cards"], 7), (shelf["tripod"], 2), (shelf["reader"], 4)])

    assert stocktake.reconcile(conn, session.id).is_clean, \
        "an open count must compare against what is on the shelf now"


def test_an_abandoned_count_records_no_findings(conn, actor, shelf):
    """A half-count is not an observation -- there is nothing to freeze.

    Its scans are kept, so reconcile falls back to computing, which is the
    most honest thing available for a count nobody finished.
    """
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session, [(shelf["cards"], 10)])
    stocktake.abandon_stocktake(conn, actor=actor, stocktake_id=session.id,
                                reason="fire alarm")

    assert conn.execute(
        "SELECT COUNT(*) FROM stocktake_result WHERE stocktake_id = ?",
        (session.id,),
    ).fetchone()[0] == 0
    assert stocktake.reconcile(conn, session.id) is not None


def test_the_stocktake_is_audited_at_both_ends(conn, actor, shelf):
    session = stocktake.start_stocktake(conn, actor=actor, scope_unit="Unit B")
    scan_all(conn, actor, session, [(shelf["cards"], 9)])
    stocktake.finish_stocktake(conn, actor=actor, stocktake_id=session.id)

    actions = [e.action for e in service.list_events(conn)]
    assert "stocktake.start" in actions
    assert "stocktake.finish" in actions
    assert service.verify_audit_chain(conn).ok


# ---------------------------------------------------------------------------
# merging duplicate people
# ---------------------------------------------------------------------------


@pytest.fixture
def twins(conn, actor):
    """The same human, entered twice under two addresses."""
    return (
        service.create_person(conn, actor=actor, name="Alice Nguyen",
                              email="alice@rit.edu"),
        service.create_person(conn, actor=actor, name="Alice Nguyen",
                              email="an1234@g.rit.edu"),
    )


def test_duplicates_are_spotted_by_name(conn, twins):
    pairs = service.possible_duplicates(conn)
    assert len(pairs) == 1
    assert {p.email for p in pairs[0]} == {"alice@rit.edu", "an1234@g.rit.edu"}


def test_merging_moves_the_loans(conn, actor, item, twins):
    keep, merge = twins
    service.checkout(conn, actor=actor, item_id=item.id, person_id=merge.id,
                     quantity=2)
    service.checkout(conn, actor=actor, item_id=item.id, person_id=keep.id,
                     quantity=1)

    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)

    assert len(service.list_loans(conn, person_id=keep.id, open_only=True)) == 2
    assert service.list_loans(conn, person_id=merge.id, open_only=True) == []


def test_nothing_is_deleted_by_a_merge(conn, actor, twins):
    keep, merge = twins
    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)

    still_there = service.get_person(conn, merge.id)
    assert still_there.is_merged
    assert still_there.merged_into_id == keep.id
    assert still_there.email == "an1234@g.rit.edu"


def test_the_merged_address_is_kept_where_someone_will_find_it(conn, actor,
                                                               twins):
    keep, merge = twins
    kept = service.merge_people(conn, actor=actor, keep_id=keep.id,
                                merge_id=merge.id, reason="same person")
    assert "an1234@g.rit.edu" in kept.notes
    assert "same person" in kept.notes


def test_a_merged_record_drops_out_of_the_pickers(conn, actor, twins):
    keep, merge = twins
    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)

    emails = [p.email for p in service.list_people(conn)]
    assert "an1234@g.rit.edu" not in emails
    # Even when asking for inactive people, which a merge also sets.
    emails = [p.email for p in service.list_people(conn, include_inactive=True)]
    assert "an1234@g.rit.edu" not in emails
    emails = [p.email for p in service.list_people(conn, include_inactive=True,
                                                   include_merged=True)]
    assert "an1234@g.rit.edu" in emails


def test_a_login_account_follows_the_merge(conn, actor, twins):
    keep, merge = twins
    admin = accounts.register(conn, first_name="Carter", last_name="L",
                              email="carter@rit.edu", password=STRONG,
                              role="admin", status="active", actor=SETUP)
    conn.execute("UPDATE account SET person_id = ? WHERE id = ?",
                 (merge.id, admin.id))
    conn.commit()

    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)

    assert accounts.get_account(conn, admin.id).person_id == keep.id


def test_a_person_cannot_be_merged_into_themselves(conn, actor, twins):
    keep, _ = twins
    with pytest.raises(ValidationError, match="same person"):
        service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=keep.id)


def test_an_already_merged_record_cannot_be_merged_again(conn, actor, twins):
    keep, merge = twins
    third = service.create_person(conn, actor=actor, name="Alice Nguyen",
                                  email="alice.n@rit.edu")
    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)

    with pytest.raises(ConflictError, match="already been merged"):
        service.merge_people(conn, actor=actor, keep_id=third.id,
                             merge_id=merge.id)
    with pytest.raises(ConflictError, match="merged record"):
        service.merge_people(conn, actor=actor, keep_id=merge.id,
                             merge_id=third.id)


def test_a_merge_is_in_the_history(conn, actor, twins):
    keep, merge = twins
    service.merge_people(conn, actor=actor, keep_id=keep.id, merge_id=merge.id)
    latest = service.list_events(conn, person_id=keep.id)[0]
    assert latest.action == "person.merge"
    assert "an1234@g.rit.edu" in latest.summary


# ---------------------------------------------------------------------------
# requests: age, and competing demand
# ---------------------------------------------------------------------------


@pytest.fixture
def requester(conn):
    admin = accounts.register(conn, first_name="Carter", last_name="L",
                              email="carter@rit.edu", password=STRONG,
                              role="admin", status="active", actor=SETUP)
    account = accounts.register(conn, first_name="Alice", last_name="Nguyen",
                                email="an1234@rit.edu",
                                password="seventeen purple bicycles",
                                actor=SETUP)
    return accounts.approve(conn, actor=admin.as_actor(), account_id=account.id,
                            approved_by=admin)


def test_a_fresh_request_is_not_stale(conn, actor, item, requester):
    filed = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=1)
    assert not filed.is_stale
    assert filed.age_label == "today"
    assert rq.count_stale(conn) == 0


def test_an_old_request_is_flagged(conn, actor, item, requester):
    filed = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=1)
    conn.execute("UPDATE request SET created_at = '2026-01-01T00:00:00Z' "
                 "WHERE id = ?", (filed.id,))
    conn.commit()

    aged = rq.get_request(conn, filed.id)
    assert aged.is_stale
    assert aged.age_days > config.REQUEST_STALE_DAYS
    assert rq.count_stale(conn) == 1


def test_overlapping_requests_for_the_same_item_are_found(conn, actor, item,
                                                          requester):
    first = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=6,
                             needed_from="2026-09-01T00:00:00Z",
                             needed_until="2026-09-05T00:00:00Z")
    rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                     item_id=item.id, quantity=6,
                     needed_from="2026-09-03T00:00:00Z",
                     needed_until="2026-09-08T00:00:00Z")

    overlaps = rq.overlapping_requests(conn, first)
    assert len(overlaps) == 1
    assert rq.competing_demand(conn, first) == 12, "more than the ten owned"


def test_requests_in_different_weeks_do_not_overlap(conn, actor, item,
                                                    requester):
    first = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=6,
                             needed_from="2026-09-01T00:00:00Z",
                             needed_until="2026-09-05T00:00:00Z")
    rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                     item_id=item.id, quantity=6,
                     needed_from="2026-10-01T00:00:00Z",
                     needed_until="2026-10-05T00:00:00Z")

    assert rq.overlapping_requests(conn, first) == []


def test_a_request_with_no_dates_overlaps_everything(conn, actor, item,
                                                     requester):
    """Open-ended means open-ended; NULL cannot be assumed to be narrow."""
    open_ended = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                                  item_id=item.id, quantity=1)
    rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                     item_id=item.id, quantity=1,
                     needed_from="2027-01-01T00:00:00Z",
                     needed_until="2027-01-02T00:00:00Z")

    assert len(rq.overlapping_requests(conn, open_ended)) == 1


def test_a_decided_request_stops_competing(conn, actor, item, requester):
    first = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=2)
    other = rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                             item_id=item.id, quantity=2)
    rq.decline(conn, actor=actor, request_id=other.id,
               decided_by_id=requester.id, note="not this time")

    assert rq.overlapping_requests(conn, first) == []


def test_overlap_reserves_nothing(conn, actor, item, requester):
    """It is advice, not a booking. Availability must not move."""
    before = service.get_item(conn, item.id).available
    rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                     item_id=item.id, quantity=8)
    rq.submit_borrow(conn, actor=actor, requester_id=requester.id,
                     item_id=item.id, quantity=8)
    assert service.get_item(conn, item.id).available == before
