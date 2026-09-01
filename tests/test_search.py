"""Search and barcode scanning."""

from stockroom import barcodes, search, service


def test_finds_by_name_prefix(conn, actor):
    service.create_item(conn, actor=actor, name="Canon EOS R5 Body")
    assert search.search_items(conn, "can")[0].name == "Canon EOS R5 Body"


def test_finds_by_description(conn, actor, item):
    assert search.search_items(conn, "UHS-I")[0].id == item.id


def test_finds_by_location(conn, actor, item):
    assert search.search_items(conn, "Bin 12")[0].id == item.id


def test_multiple_terms_are_all_required(conn, actor):
    service.create_item(conn, actor=actor, name="Canon Lens", unit="Unit A")
    service.create_item(conn, actor=actor, name="Nikon Lens", unit="Unit B")
    assert len(search.search_items(conn, "lens")) == 2
    assert len(search.search_items(conn, "canon lens")) == 1


def test_punctuation_does_not_break_the_query(conn, actor):
    service.create_item(conn, actor=actor, name="Canon EOS-R5 (body)")
    # These are FTS5 syntax characters; they must not raise.
    for query in ["EOS-R5", "(body)", 'a "quoted', "*", "^^^"]:
        search.search_items(conn, query)


def test_empty_query_returns_nothing(conn, item):
    assert search.search_items(conn, "") == []
    assert search.search_items(conn, "   ") == []


def test_archived_items_are_hidden_by_default(conn, actor, item):
    service.archive_item(conn, actor=actor, item_id=item.id)
    assert search.search_items(conn, "SanDisk") == []
    assert len(search.search_items(conn, "SanDisk", include_archived=True)) == 1


def test_search_index_follows_edits(conn, actor, item):
    service.update_item(conn, actor=actor, item_id=item.id, name="Renamed Widget")
    assert search.search_items(conn, "SanDisk") == []
    assert search.search_items(conn, "Renamed")[0].id == item.id


def test_scanning_a_barcode_resolves_exactly(conn, item):
    assert search.resolve_scan(conn, item.barcode).id == item.id


def test_scanning_is_case_insensitive(conn, item):
    assert search.resolve_scan(conn, item.barcode.lower()).id == item.id


def test_an_unambiguous_text_scan_resolves(conn, item):
    assert search.resolve_scan(conn, "SanDisk").id == item.id


def test_an_ambiguous_scan_resolves_to_nothing(conn, actor):
    """Better to show a list than to jump to the wrong item."""
    service.create_item(conn, actor=actor, name="Cable HDMI")
    service.create_item(conn, actor=actor, name="Cable USB")
    assert search.resolve_scan(conn, "Cable") is None


def test_unknown_code_resolves_to_nothing(conn, item):
    assert search.resolve_scan(conn, "NOPE-999999") is None


def test_barcode_shape_detection():
    assert search.looks_like_barcode("CIS-000142")
    assert search.looks_like_barcode("7630049200371")
    assert not search.looks_like_barcode("Canon EOS")


def test_barcode_svg_is_renderable(item):
    svg = barcodes.render_svg(item.barcode)
    assert svg.startswith("<svg")
    assert "mm" in svg          # physical dimensions preserved for printing
    assert barcodes.render_svg("") == ""


# ---------------------------------------------------------------------------
# asset tags
#
# Scanning an item barcode says what kind of thing it is; scanning an asset
# tag says which one. Only the stocktake understood the second, so the counter
# and the CLI answered "nothing matched" for a tag the stocktake accepted.
# ---------------------------------------------------------------------------


def _tracked_camera(conn, actor):
    camera = service.create_item(conn, actor=actor, name="Canon EOS R5",
                                 quantity=2, tracked=True)
    unit = service.create_unit(conn, actor=actor, item_id=camera.id,
                               asset_tag="CIS-U-7", serial="SN0007")
    return camera, unit


def test_an_asset_tag_resolves_to_its_item_and_unit(conn, actor):
    camera, unit = _tracked_camera(conn, actor)

    found = search.resolve(conn, "CIS-U-7")
    assert found is not None
    assert found.item.id == camera.id
    assert found.unit is not None and found.unit.id == unit.id
    assert found.names_a_unit


def test_an_asset_tag_scan_is_case_insensitive_like_a_barcode(conn, actor):
    _tracked_camera(conn, actor)
    found = search.resolve(conn, "cis-u-7")
    assert found is not None and found.names_a_unit


def test_an_item_barcode_resolves_without_naming_a_unit(conn, item):
    found = search.resolve(conn, item.barcode)
    assert found is not None
    assert found.item.id == item.id
    assert found.unit is None
    assert not found.names_a_unit


def test_resolve_scan_still_answers_with_the_item(conn, actor):
    """The old entry point keeps its shape for callers that only want the item."""
    camera, _ = _tracked_camera(conn, actor)
    assert search.resolve_scan(conn, "CIS-U-7").id == camera.id


def test_an_unknown_code_resolves_to_nothing(conn, item):
    assert search.resolve(conn, "NOT-A-THING-AT-ALL") is None
    assert search.resolve(conn, "") is None
