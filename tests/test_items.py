"""Items: creation, editing, quantity rules, archiving, barcodes."""

import pytest

from stockroom import service
from stockroom.service import ConflictError, NotFound, ValidationError


def test_create_assigns_sequential_barcodes(conn, actor):
    first = service.create_item(conn, actor=actor, name="Item A")
    second = service.create_item(conn, actor=actor, name="Item B")
    assert first.barcode == "CIS-000001"
    assert second.barcode == "CIS-000002"


def test_create_accepts_a_manufacturer_barcode(conn, actor):
    item = service.create_item(conn, actor=actor, name="Arduino", barcode="7630049200371")
    assert item.barcode == "7630049200371"


def test_duplicate_barcode_is_rejected(conn, actor):
    service.create_item(conn, actor=actor, name="A", barcode="DUP-1")
    with pytest.raises(ConflictError, match="already used"):
        service.create_item(conn, actor=actor, name="B", barcode="DUP-1")


def test_name_is_required(conn, actor):
    with pytest.raises(ValidationError):
        service.create_item(conn, actor=actor, name="   ")


def test_location_skips_empty_parts(conn, actor):
    item = service.create_item(conn, actor=actor, name="X", unit="Unit A", shelf="Shelf 1")
    assert item.location == "Unit A / Shelf 1"
    bare = service.create_item(conn, actor=actor, name="Y")
    assert bare.location == "Unassigned"


def test_a_new_item_is_fully_available(item):
    assert item.quantity == 10
    assert item.available == 10
    assert item.out_qty == 0
    assert item.status_label == "Available"


def test_unknown_field_is_rejected(conn, actor, item):
    with pytest.raises(ValidationError, match="Unknown item field"):
        service.update_item(conn, actor=actor, item_id=item.id, colour="red")


def test_updating_nothing_writes_no_event(conn, actor, item):
    before = service.count_events(conn)
    service.update_item(conn, actor=actor, item_id=item.id, name=item.name)
    assert service.count_events(conn) == before


def test_relocation_is_logged_as_relocate(conn, actor, item):
    service.update_item(conn, actor=actor, item_id=item.id, shelf="Shelf 9")
    latest = service.list_events(conn, item_id=item.id, limit=1)[0]
    assert latest.action == "item.relocate"
    assert "Shelf 3" in latest.summary and "Shelf 9" in latest.summary


def test_quantity_change_is_logged_as_adjust(conn, actor, item):
    service.update_item(conn, actor=actor, item_id=item.id, quantity=15)
    latest = service.list_events(conn, item_id=item.id, limit=1)[0]
    assert latest.action == "item.quantity_adjust"
    assert latest.changes["quantity"] == {"from": 10, "to": 15}


def test_quantity_cannot_drop_below_units_on_loan(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=4)
    with pytest.raises(ConflictError, match="currently checked out"):
        service.update_item(conn, actor=actor, item_id=item.id, quantity=3)
    # ...but reducing to exactly the number out is fine.
    updated = service.update_item(conn, actor=actor, item_id=item.id, quantity=4)
    assert updated.available == 0


def test_archive_requires_everything_back(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=1)
    with pytest.raises(ConflictError, match="still checked out"):
        service.archive_item(conn, actor=actor, item_id=item.id)


def test_archive_and_restore_round_trip(conn, actor, item):
    archived = service.archive_item(conn, actor=actor, item_id=item.id, reason="broken")
    assert archived.is_archived
    assert archived.id not in {i.id for i in service.list_items(conn)}
    restored = service.restore_item(conn, actor=actor, item_id=item.id)
    assert not restored.is_archived
    assert restored.id in {i.id for i in service.list_items(conn)}


def test_archived_items_cannot_be_checked_out(conn, actor, item, person):
    service.archive_item(conn, actor=actor, item_id=item.id)
    with pytest.raises(ConflictError, match="archived"):
        service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id)


def test_low_stock_tracks_availability_not_total(conn, actor, item, person):
    assert not item.is_low_stock
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=8)
    assert service.get_item(conn, item.id).is_low_stock
    assert service.list_items(conn, only_low_stock=True)[0].id == item.id


def test_assign_barcode_to_an_item_without_one(conn, actor):
    item = service.create_item(conn, actor=actor, name="Unlabelled", generate_barcode=False)
    assert item.barcode is None
    updated = service.assign_barcode(conn, actor=actor, item_id=item.id)
    assert updated.barcode == "CIS-000001"
    with pytest.raises(ConflictError, match="already has barcode"):
        service.assign_barcode(conn, actor=actor, item_id=item.id)


def test_missing_item_raises_not_found(conn):
    with pytest.raises(NotFound):
        service.get_item(conn, 9999)


def test_a_product_link_must_be_http(conn, actor):
    """`<input type="url">` is a hint to the browser, not a check.

    A javascript: URL reached the href on the item page and, through the
    embedded JSON, the generated public page too. Both are saved today by a
    CSP with no 'unsafe-inline' -- but the public page is meant to be opened
    from a USB stick, where its own <meta> policy is all there is.
    """
    for bad in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x"):
        with pytest.raises(ValidationError, match="http"):
            service.create_item(conn, actor=actor, name="Bad link", product_url=bad)

    ok = service.create_item(conn, actor=actor, name="Good link",
                             product_url="https://example.test/thing")
    assert ok.product_url == "https://example.test/thing"

    with pytest.raises(ValidationError, match="http"):
        service.update_item(conn, actor=actor, item_id=ok.id,
                            product_url="javascript:alert(1)")
    assert service.get_item(conn, ok.id).product_url == "https://example.test/thing"


def test_free_text_is_bounded(conn, actor, item):
    """There was no limit on any text field, anywhere.

    Not in the schema, which is all TEXT; not in the routes; not in the
    validation helpers. An approved requester gets twenty submissions an hour
    and every field took whatever fitted in a 9 MB body, onto an SD card. The
    caps are generous enough that nobody writing in good faith meets them.
    """
    with pytest.raises(ValidationError, match="too long"):
        service.create_item(conn, actor=actor, name="x" * (service.MAX_NAME + 1))
    with pytest.raises(ValidationError, match="too long"):
        service.create_item(conn, actor=actor, name="Fine",
                            description="x" * (service.MAX_TEXT + 1))
    with pytest.raises(ValidationError, match="too long"):
        service.update_item(conn, actor=actor, item_id=item.id,
                            description="x" * (service.MAX_TEXT + 1))
    with pytest.raises(ValidationError, match="too long"):
        service.create_person(conn, actor=actor, name="Real Person",
                              email="rp@rit.edu",
                              notes="x" * (service.MAX_TEXT + 1))

    # And the ordinary case is untouched.
    ok = service.create_item(conn, actor=actor, name="y" * service.MAX_NAME,
                             description="z" * service.MAX_TEXT)
    assert len(ok.name) == service.MAX_NAME
