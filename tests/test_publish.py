"""Rendering the public page, debouncing, and failure isolation."""

import json
import time

import pytest

from stockroom import config, service
from stockroom.publish import publishers, render, worker


@pytest.fixture
def stocked(conn, actor, item, person):
    service.checkout(conn, actor=actor, item_id=item.id, person_id=person.id, quantity=3)
    return conn


def test_renders_both_files(stocked):
    files = render.render_site(stocked)
    assert set(files) == {"index.html", "inventory.json"}
    assert files["index.html"].lstrip().startswith("<!DOCTYPE html>")


def test_json_feed_is_valid_and_complete(stocked):
    payload = json.loads(render.render_json(stocked))
    assert payload["organization"] == config.ORG_NAME
    entry = payload["items"][0]
    assert entry["available"] == 7
    assert entry["quantity"] == 10
    assert entry["out_qty"] == 3
    assert entry["location"] == "Unit B / Shelf 3 / Bin 12"


def test_borrowers_are_not_published(stocked):
    """The privacy default: counts are public, people are not."""
    files = render.render_site(stocked)
    for contents in files.values():
        assert "alice@rit.edu" not in contents
        assert "Alice Nguyen" not in contents


def test_borrowers_appear_only_when_explicitly_enabled(stocked, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_SHOW_BORROWERS", True)
    payload = json.loads(render.render_json(stocked))
    assert payload["loans"][0]["email"] == "alice@rit.edu"


def test_archived_items_are_not_published(conn, actor, item):
    service.archive_item(conn, actor=actor, item_id=item.id)
    payload = json.loads(render.render_json(conn))
    assert payload["items"] == []


def _embedded_items(html: str):
    """Pull the JSON the page hands to its own script, exactly as a browser would.

    A browser reads a <script> element's contents as raw text -- HTML
    entities are NOT decoded there -- so this parses the block verbatim. If
    the template ever HTML-escapes the payload again, this fails the way the
    real page would.
    """
    import json as _json
    import re

    match = re.search(
        r'<script id="data" type="application/json">(.*?)</script>', html, re.S
    )
    assert match, "the page has no embedded data block"
    return _json.loads(match.group(1))


def test_the_embedded_data_is_parseable_json(stocked):
    """Guards the bug where autoescaping turned the payload into &#34; entities."""
    items = _embedded_items(render.render_site(stocked)["index.html"])
    assert items[0]["name"] == "SanDisk 64GB SD Card"
    assert items[0]["available"] == 7
    assert "&#34;" not in render.render_site(stocked)["index.html"]


def test_html_cannot_be_broken_out_of_by_item_text(conn, actor):
    """An item description is embedded in a <script> block; it must not escape."""
    service.create_item(conn, actor=actor, name="Sneaky",
                        description="</script><script>alert(1)</script>")
    html = render.render_site(conn)["index.html"]

    # The literal closing tag never appears inside the data block...
    assert "</script><script>alert(1)" not in html
    # ...but the value still round-trips intact to the page's own script.
    items = _embedded_items(html)
    assert items[0]["description"] == "</script><script>alert(1)</script>"


def test_local_publisher_writes_the_files(stocked, tmp_path):
    target = tmp_path / "site"
    publishers.LocalPublisher(target).publish(render.render_site(stocked))
    assert (target / "index.html").exists()
    assert (target / "inventory.json").exists()


def test_published_files_are_world_readable(stocked, tmp_path):
    """A web server running as another user has to be able to read them."""
    target = tmp_path / "site"
    publishers.LocalPublisher(target).publish(render.render_site(stocked))
    assert (target / "index.html").stat().st_mode & 0o044


def test_publishing_is_atomic(stocked, tmp_path):
    """No temp files are left behind, so nothing serves a half-written page."""
    target = tmp_path / "site"
    publisher = publishers.LocalPublisher(target)
    for _ in range(3):
        publisher.publish(render.render_site(stocked))
    assert sorted(p.name for p in target.iterdir()) == ["index.html", "inventory.json"]


def test_a_burst_of_changes_publishes_once(conn, actor, tmp_path):
    """The debounce is what keeps a 200-row import from rendering 200 times."""
    published = worker.PublishWorker(
        [publishers.LocalPublisher(tmp_path / "site")],
        debounce=0.15, db_path=config.DB_PATH,
    )
    worker.install(published)
    for index in range(6):
        service.create_item(conn, actor=actor, name=f"Item {index}")
    time.sleep(0.6)
    assert published.publish_count == 1


def test_a_broken_publisher_cannot_block_a_checkout(conn, actor, item, person, tmp_path):
    """The whole point of publishing off-thread: it must never fail a change."""

    class Broken:
        name = "broken"

        def publish(self, files):
            raise RuntimeError("remote unreachable")

    healthy = publishers.LocalPublisher(tmp_path / "site")
    published = worker.PublishWorker([Broken(), healthy], debounce=0.05,
                                     db_path=config.DB_PATH)
    worker.install(published)

    loan = service.checkout(conn, actor=actor, item_id=item.id,
                            person_id=person.id, quantity=1)
    time.sleep(0.4)

    assert loan.id is not None                      # the checkout succeeded
    assert service.get_item(conn, item.id).available == 9
    assert isinstance(published.last_error, RuntimeError)
    assert (tmp_path / "site" / "index.html").exists()   # healthy one still ran


def test_rendering_is_deterministic(stocked):
    first = render.render_json(stocked)
    second = render.render_json(stocked)
    # Only the generation timestamp may differ.
    strip = lambda text: [l for l in text.splitlines() if "generated_at" not in l]
    assert strip(first) == strip(second)


def test_an_unwritable_directory_does_not_break_a_change(conn, actor, item, person, tmp_path):
    """The realistic version of a publisher failure: a read-only directory."""
    locked = tmp_path / "readonly"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        published = worker.PublishWorker(
            [publishers.LocalPublisher(locked)], debounce=0.05, db_path=config.DB_PATH
        )
        worker.install(published)

        loan = service.checkout(conn, actor=actor, item_id=item.id,
                                person_id=person.id, quantity=2)
        time.sleep(0.35)

        assert loan.id is not None
        assert service.get_item(conn, item.id).available == 8
        assert isinstance(published.last_error, PermissionError)

        # ...and it recovers once the directory is writable again.
        locked.chmod(0o700)
        published.publish()
        assert (locked / "index.html").exists()
    finally:
        locked.chmod(0o700)


def test_a_failed_publish_leaves_no_temp_files(conn, actor, item, tmp_path):
    """A publish that dies partway must not litter the served directory.

    Each file is written to a temp file and renamed, so a reader sees either
    the old file or the new one -- never a half-written page, and never a
    stray dotfile that accumulates on every failure.
    """
    target = tmp_path / "site"
    publisher = publishers.LocalPublisher(target)
    publisher.publish(render.render_site(conn))

    class Exploding(dict):
        def items(self):
            yield "index.html", "<!DOCTYPE html><p>partial"
            raise RuntimeError("died mid-publish")

    with pytest.raises(RuntimeError):
        publisher.publish(Exploding())

    assert sorted(p.name for p in target.iterdir()) == ["index.html", "inventory.json"]
    # Whatever landed is a complete file, not a truncated write.
    assert (target / "index.html").read_text() == "<!DOCTYPE html><p>partial"
