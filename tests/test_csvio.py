"""CSV import and export."""

import pytest

from stockroom import csvio, service
from stockroom.service import ValidationError

SAMPLE = """name,description,quantity,unit,shelf,sub_location,min_quantity
Canon EOS R5,Mirrorless body,2,Unit A,Shelf 1,Pelican,1
SD Card,64GB UHS-I,12,Unit B,Shelf 3,Bin 12,4
"""


def test_dry_run_changes_nothing(conn, actor):
    result = csvio.import_csv(conn, SAMPLE, actor=actor, commit=False)
    assert len(result.created) == 2
    assert result.committed is False
    assert service.list_items(conn) == []


def test_commit_creates_the_items(conn, actor):
    result = csvio.import_csv(conn, SAMPLE, actor=actor, commit=True)
    assert result.committed is True
    items = service.list_items(conn)
    assert len(items) == 2
    assert {i.name for i in items} == {"Canon EOS R5", "SD Card"}
    assert all(i.barcode for i in items), "every imported item gets a barcode"


def test_reimport_updates_rather_than_duplicating(conn, actor):
    csvio.import_csv(conn, SAMPLE, actor=actor, commit=True)
    result = csvio.import_csv(conn, SAMPLE, actor=actor, commit=True)
    assert result.created == []
    assert len(result.skipped) == 2
    assert len(service.list_items(conn)) == 2


def test_an_edited_sheet_updates_in_place(conn, actor):
    csvio.import_csv(conn, SAMPLE, actor=actor, commit=True)
    edited = SAMPLE.replace("64GB UHS-I,12", "64GB UHS-I,20")
    result = csvio.import_csv(conn, edited, actor=actor, commit=True)
    assert len(result.updated) == 1
    card = [i for i in service.list_items(conn) if i.name == "SD Card"][0]
    assert card.quantity == 20


def test_one_bad_row_abandons_the_whole_file(conn, actor):
    bad = SAMPLE + "Broken Item,desc,notanumber,Unit C,Shelf 1,,\n"
    result = csvio.import_csv(conn, bad, actor=actor, commit=True)
    assert not result.ok
    assert result.committed is False
    assert service.list_items(conn) == []


def test_header_aliases_are_accepted(conn, actor):
    text = "Item Name,Qty,Storage Unit,Shelf,Bin,Product Link\nTripod,4,Unit A,Floor,,https://x.test\n"
    result = csvio.import_csv(conn, text, actor=actor, commit=True)
    assert len(result.created) == 1
    item = service.list_items(conn)[0]
    assert item.name == "Tripod"
    assert item.quantity == 4
    assert item.product_url == "https://x.test"


def test_unknown_columns_are_reported_not_fatal(conn, actor):
    text = "name,quantity,Colour\nThing,2,red\n"
    result = csvio.import_csv(conn, text, actor=actor, commit=True)
    assert result.ok
    assert result.ignored_columns == ["Colour"]


def test_a_name_column_is_required(conn, actor):
    with pytest.raises(ValidationError, match="name"):
        csvio.import_csv(conn, "quantity,unit\n3,Unit A\n", actor=actor)


def test_an_empty_file_is_rejected(conn, actor):
    with pytest.raises(ValidationError, match="empty"):
        csvio.import_csv(conn, "", actor=actor)


def test_import_writes_an_audit_event(conn, actor):
    csvio.import_csv(conn, SAMPLE, actor=actor, commit=True, source="stock.csv")
    actions = [e.action for e in service.list_events(conn)]
    assert "import.run" in actions
    assert actions.count("item.create") == 2


def test_export_round_trips(conn, actor):
    csvio.import_csv(conn, SAMPLE, actor=actor, commit=True)
    exported = csvio.export_csv(conn)
    result = csvio.import_csv(conn, exported, actor=actor, commit=True)
    # Re-importing our own export is a no-op, not a duplication.
    assert result.created == []
    assert len(service.list_items(conn)) == 2


def test_a_name_that_looks_like_a_formula_is_exported_as_text(conn, actor):
    """Otherwise the export runs code on the machine that opens it.

    Excel and LibreOffice both execute a cell beginning `=`, `+`, `-` or `@`.
    Item names are free text and not all of it is staff-written -- a
    requester's new-item request supplies the name staff create the item from.
    """
    service.create_item(conn, actor=actor, name="=HYPERLINK(\"http://evil.test\")",
                        quantity=1, unit="Unit A", shelf="Shelf 1")
    exported = csvio.export_csv(conn)
    row = [l for l in exported.splitlines() if "HYPERLINK" in l][0]
    assert "'=HYPERLINK" in row, "a formula-leading cell must be quoted as text"
    assert not row.startswith("=HYPERLINK")


def test_defusing_a_formula_survives_the_round_trip(conn, actor):
    """The apostrophe is display armour, not part of the name."""
    original = "=1+1"
    service.create_item(conn, actor=actor, name=original, quantity=1,
                        unit="Unit A", shelf="Shelf 1")
    reimported = csvio.import_csv(
        conn, csvio.export_csv(conn), actor=actor, commit=True
    )
    assert reimported.created == []
    assert original in [i.name for i in service.list_items(conn)]


def test_export_reports_live_availability(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=4)
    exported = csvio.export_csv(conn)
    row = [l for l in exported.splitlines() if "SanDisk" in l][0]
    assert ",6," in row      # available
    assert "Partially out" in row
