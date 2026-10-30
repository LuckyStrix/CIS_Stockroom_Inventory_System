"""The USB label printer: the raster it sends, and the web flow around it.

No test touches a real printer. ``FakePrinter`` stands in for the device and
answers status requests the way a QL-500 does; temp_env points
STOCKROOM_LABEL_PRINTER somewhere that does not exist, so even on the Pi with
the printer plugged in, the suite cannot print.
"""

import re
import struct
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from stockroom import accounts, config, db, diagnostics, labelprinter, service
from stockroom.service import Actor

SETUP = Actor("cli:test")
STAFF_PASSWORD = "glass onion tuesday lamp"
TAPE_62 = labelprinter.TAPES[(62, 0)]


def status_reply(*, width=62, length=0, kind=0x0A, err1=0, err2=0, type_=0x00):
    reply = bytearray(32)
    reply[0:4] = b"\x80\x20B0"
    reply[8], reply[9] = err1, err2
    reply[10], reply[11], reply[17], reply[18] = width, kind, length, type_
    return bytes(reply)


class FakePrinter:
    """A QL-500 as far as this module can tell: status in, completions out."""

    instances: list["FakePrinter"] = []

    def __init__(self, *, status=None, fail_after=None):
        self.status = status or status_reply()
        self.fail_after = fail_after
        self.writes: list[bytes] = []
        self.replies: list[bytes] = []
        self.closed = False

    # the _Device interface
    def write(self, data):
        self.writes.append(bytes(data))
        if data.endswith(b"\x1biS"):
            self.replies.append(self.status)
            return
        for page in range(pages_in(data)):
            if self.fail_after is not None and page >= self.fail_after:
                self.replies.append(status_reply(err2=0x10, type_=0x02))
                return
            self.replies.append(status_reply(type_=0x06))
            self.replies.append(status_reply(type_=0x01))

    def read_status(self, timeout):
        return self.replies.pop(0) if self.replies else None

    def close(self):
        self.closed = True

    @property
    def jobs(self):
        return [w for w in self.writes if not w.endswith(b"\x1biS")]


def pages_in(job: bytes) -> int:
    return job.count(b"\x1biz")


@pytest.fixture
def printer(monkeypatch, tmp_path):
    """A connected printer: the device path exists and talks like a QL-500."""
    device = tmp_path / "stockroom-label"
    device.touch()
    monkeypatch.setattr(config, "LABEL_PRINTER", str(device))
    fake = FakePrinter()
    monkeypatch.setattr(labelprinter, "_connect", lambda path, wait: fake)
    return fake


def run_all_threads():
    for thread in threading.enumerate():
        if thread.name == "label-printer":
            thread.join(timeout=10)


# ---------------------------------------------------------------------------
# what goes over the wire
# ---------------------------------------------------------------------------


def test_a_job_is_framed_the_way_the_printer_expects():
    images = [labelprinter.render(labelprinter.Label("CIS-000001", "One"), TAPE_62),
              labelprinter.render(labelprinter.Label("CIS-000002", "Two"), TAPE_62)]
    job = labelprinter.encode(images, TAPE_62)

    assert job.startswith(b"\x00" * 200 + b"\x1b@")
    headers = [m.start() for m in re.finditer(rb"\x1biz", job)]
    assert len(headers) == 2
    for page, (at, image) in enumerate(zip(headers, images)):
        flags, kind, width, length = job[at + 3:at + 7]
        lines, = struct.unpack("<L", job[at + 7:at + 11])
        assert (kind, width, length) == (0x0A, 62, 0)
        assert lines == image.height
        assert job[at + 11] == (0 if page == 0 else 1)   # first page, or not
    # Form feed between pages, Ctrl-Z at the very end and nowhere else.
    assert job.endswith(b"\x1a")
    rows = sum(i.height for i in images)
    assert job.count(b"g\x00\x5a") == rows               # 90-byte raster lines


def test_the_raster_is_mirrored_and_offset_for_the_tape():
    """A dot at the label's left edge lands where Brother says it should.

    The bitmap goes into the 720-dot head right-aligned less the tape's
    margin, then mirrored. Get either wrong and the label prints off the
    edge of the tape -- silently, since the printer cannot tell.
    """
    from PIL import Image

    image = Image.new("L", (TAPE_62.across, 1), 255)
    image.putpixel((0, 0), 0)
    job = labelprinter.encode([image], TAPE_62)
    row = job[job.index(b"g\x00\x5a") + 3:][:90]

    head_x = 720 - 1 - (720 - TAPE_62.across - TAPE_62.margin)
    expected = bytearray(90)
    expected[head_x // 8] = 0x80 >> (head_x % 8)
    assert row == bytes(expected)


def test_the_bars_are_whole_dots_and_spell_the_code():
    """Read the barcode back off the rendered label, bar by bar."""
    label = labelprinter.Label("CIS-000142", "Thorlabs KM100", "Unit B / Shelf 3")
    image = labelprinter.render(label, TAPE_62)
    width, height = image.size

    # The row through the middle of the bars: scan down for the first row
    # that is pure black and white with more than a few bars in it.
    for y in range(height):
        row = [image.getpixel((x, y)) for x in range(width)]
        if set(row) <= {0, 255} and sum(1 for a, b in zip(row, row[1:]) if a != b) > 40:
            break
    else:
        pytest.fail("found no row of bars")

    runs, current, length = [], row[0], 0
    for value in row:
        if value == current:
            length += 1
        else:
            runs.append((current, length))
            current, length = value, 1
    runs.append((current, length))
    runs = runs[1:-1]                                     # drop the quiet zones
    module = min(n for _, n in runs)
    assert module in (2, 3, 4)
    assert all(n % module == 0 for _, n in runs)
    pattern = "".join(("1" if v == 0 else "0") * (n // module) for v, n in runs)
    assert pattern == labelprinter._modules("CIS-000142")


def test_the_bars_are_eight_millimetres_tall():
    image = labelprinter.render(labelprinter.Label("CIS-000142", "x"), TAPE_62)

    def longest_black_run(x):
        best = run = 0
        for y in range(image.height):
            run = run + 1 if image.getpixel((x, y)) == 0 else 0
            best = max(best, run)
        return best

    # 94 dots is 8 mm at 300 dpi -- half what the first version drew.
    assert max(longest_black_run(x) for x in range(image.width)) == 94


@pytest.mark.parametrize("key", sorted(labelprinter.TAPES))
def test_every_supported_tape_gets_a_label_of_its_width(key):
    tape = labelprinter.TAPES[key]
    image = labelprinter.render(
        labelprinter.Label("CIS-000142", "A rather long item name for a label", "Unit A / 3"),
        tape,
    )
    assert image.width == tape.across
    if not tape.continuous:
        assert image.height == tape.along
    labelprinter.encode([image], tape)    # and it encodes


def test_a_code_too_long_to_scan_is_refused_not_printed_tiny():
    with pytest.raises(labelprinter.LabelPrinterError, match="too long"):
        labelprinter.render(labelprinter.Label("X" * 60, "x"), TAPE_62)


def test_a_code_code128_cannot_carry_is_refused():
    with pytest.raises(labelprinter.LabelPrinterError):
        labelprinter.render(labelprinter.Label("CAMÉRA-1", "x"), TAPE_62)


# ---------------------------------------------------------------------------
# talking to the printer
# ---------------------------------------------------------------------------


def test_status_says_what_is_wrong_in_plain_words():
    status = labelprinter._parse_status(status_reply(err2=0x10))
    with pytest.raises(labelprinter.LabelPrinterError, match="cover is open"):
        status.usable_tape()


def test_tape_too_small_for_a_label_is_refused():
    status = labelprinter._parse_status(status_reply(width=12))
    with pytest.raises(labelprinter.LabelPrinterError, match="too small"):
        status.usable_tape()


def test_print_waits_for_every_page(printer):
    count = labelprinter.print_labels(
        [labelprinter.Label("CIS-000001", "One"), labelprinter.Label("CIS-000002", "Two")])
    assert count == 2
    assert [pages_in(job) for job in printer.jobs] == [2]
    assert printer.closed


def test_an_error_part_way_through_is_reported(printer):
    printer.fail_after = 1
    with pytest.raises(labelprinter.LabelPrinterError, match="cover is open after 1 of 2"):
        labelprinter.print_labels(
            [labelprinter.Label("CIS-000001", "One"), labelprinter.Label("CIS-000002", "Two")])


def test_nothing_is_sent_when_the_printer_has_no_tape(printer):
    printer.status = status_reply(err1=0x01)
    with pytest.raises(labelprinter.LabelPrinterError, match="no tape"):
        labelprinter.print_labels([labelprinter.Label("CIS-000001", "One")])
    assert printer.jobs == []
    assert printer.closed


def test_a_second_user_of_the_device_waits_its_turn(tmp_path):
    """flock() on the device: doctor must not flush a job the app is sending."""
    device = tmp_path / "lp"
    device.touch()
    first = labelprinter._Device(device, wait=0)
    try:
        with pytest.raises(labelprinter.LabelPrinterError, match="busy"):
            labelprinter._Device(device, wait=0.2)
    finally:
        first.close()
    labelprinter._Device(device, wait=0).close()     # free again


def test_switched_off_is_not_an_error_anywhere(monkeypatch):
    monkeypatch.setattr(config, "LABEL_PRINTER", "")
    assert not labelprinter.connected()
    assert diagnostics.check_label_printer().status == diagnostics.OK


def test_doctor_is_quiet_without_a_printer():
    check = diagnostics.check_label_printer()
    assert check.status == diagnostics.OK
    assert "none connected" in check.detail


def test_doctor_warns_when_the_printer_needs_a_person(printer):
    printer.status = status_reply(err2=0x10)
    check = diagnostics.check_label_printer()
    assert check.status == diagnostics.WARN
    assert "cover is open" in check.detail


def test_doctor_names_the_tape_when_all_is_well(printer):
    check = diagnostics.check_label_printer()
    assert check.status == diagnostics.OK
    assert "62 mm continuous" in check.detail


# ---------------------------------------------------------------------------
# the web flow
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
        response = test_client.post(
            "/login",
            data={"email": "operator@rit.edu", "password": STAFF_PASSWORD,
                  "next": "/", "_csrf": csrf(test_client, "/login")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield test_client


def csrf(client, path):
    match = re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text)
    return match.group(1) if match else ""


@pytest.fixture
def shelf(client, conn_for_client):
    """Three labelled items over two shelves of one cabinet, and one elsewhere."""
    conn = conn_for_client
    make = lambda name, unit, shelf, **kw: service.create_item(  # noqa: E731
        conn, actor=SETUP, name=name, unit=unit, shelf=shelf, **kw)
    return {
        "b2": make("Beamsplitter", "Cabinet A", "Shelf 2"),
        "a1": make("Zoom lens", "Cabinet A", "Shelf 1"),
        "a1b": make("Aperture", "Cabinet A", "Shelf 1"),
        "other": make("Tripod", "Cabinet B", "Floor"),
        "nocode": make("Loose screws", "Cabinet A", "Shelf 1", generate_barcode=False),
    }


@pytest.fixture
def conn_for_client(client):
    return db.connect()


def test_the_chooser_narrows_to_a_shelf_in_shelf_order(client, shelf):
    page = client.get("/labels/choose?unit=Cabinet+A").text
    # Shelf 1 (by name within it), then Shelf 2 -- the order you walk it in.
    at = [page.index(name) for name in ("Aperture", "Zoom lens", "Beamsplitter")]
    assert at == sorted(at)
    assert "Tripod" not in page and "Loose screws" not in page

    page = client.get("/labels/choose?unit=Cabinet+A&shelf=Shelf+2").text
    assert "Beamsplitter" in page and "Zoom lens" not in page


def test_the_chooser_can_include_units_by_asset_tag(client, shelf, conn_for_client):
    camera = service.create_item(conn_for_client, actor=SETUP, name="Nikon D850",
                                 unit="Cabinet A", shelf="Shelf 1", tracked=True)
    service.create_unit(conn_for_client, actor=SETUP, item_id=camera.id,
                        asset_tag="AT-0042", serial="3012345")
    assert "AT-0042" not in client.get("/labels/choose?unit=Cabinet+A").text
    assert "AT-0042" in client.get("/labels/choose?unit=Cabinet+A&units=1").text


def test_without_a_printer_the_chooser_offers_the_sheet_only(client, shelf):
    page = client.get("/labels/choose?unit=Cabinet+A").text
    assert "Print the ticked labels" not in page
    assert "/labels?ids=" in page


def test_a_batch_prints_one_label_per_press(client, shelf, printer):
    picks = [f"i{shelf[k].id}" for k in ("a1b", "a1", "b2")]
    token = csrf(client, "/labels/choose?unit=Cabinet+A")
    response = client.post("/labels/print", data={
        "_csrf": token, "pick": picks, "next": "/labels/choose?unit=Cabinet+A",
    }, follow_redirects=False)

    for expected_done in (1, 2):
        assert response.status_code == 303
        target = response.headers["location"]
        assert target.startswith("/labels/run?")
        params = parse_qs(urlparse(target).query)
        assert params["done"] == [str(expected_done)]
        assert [pages_in(j) for j in printer.jobs] == [1] * expected_done

        run_page = client.get(target).text
        assert "Print next" in run_page
        response = client.post("/labels/print", data={
            "_csrf": csrf(client, target),
            "queue": params["queue"][0], "done": params["done"][0],
            "total": params["total"][0], "next": params["next"][0],
        }, follow_redirects=False)

    assert response.headers["location"].startswith("/labels/choose?unit=Cabinet")
    assert "Printed+3" in response.headers["location"] or "Printed%203" in response.headers["location"]
    assert [pages_in(j) for j in printer.jobs] == [1, 1, 1]


def test_a_printer_problem_keeps_the_label_at_the_head_of_the_queue(client, shelf, printer):
    printer.status = status_reply(err2=0x10)
    picks = [f"i{shelf['a1'].id}", f"i{shelf['b2'].id}"]
    response = client.post("/labels/print", data={
        "_csrf": csrf(client, "/labels/choose"), "pick": picks,
    }, follow_redirects=False)
    target = response.headers["location"]
    params = parse_qs(urlparse(target).query)
    assert params["queue"] == [",".join(picks)]
    assert "cover" in params["error"][0]
    assert printer.jobs == []


def test_print_the_rest_sends_one_strip(client, shelf, printer):
    queue = ",".join(f"i{shelf[k].id}" for k in ("a1b", "a1", "b2"))
    response = client.post("/labels/print-rest", data={
        "_csrf": csrf(client, "/labels/choose"), "queue": queue, "next": "/items",
    }, follow_redirects=False)
    assert response.status_code == 303
    run_all_threads()
    assert [pages_in(j) for j in printer.jobs] == [3]


def test_the_item_page_prints_its_own_label(client, shelf, printer):
    item = shelf["a1"]
    page = client.get(f"/items/{item.id}").text
    assert 'action="/labels/print"' in page
    response = client.post("/labels/print", data={
        "_csrf": csrf(client, f"/items/{item.id}"), "pick": f"i{item.id}",
        "next": f"/items/{item.id}",
    }, follow_redirects=False)
    assert response.headers["location"].startswith(f"/items/{item.id}?ok=")
    assert len(printer.jobs) == 1


def test_the_item_page_offers_no_print_button_without_a_printer(client, shelf):
    assert 'action="/labels/print"' not in client.get(f"/items/{shelf['a1'].id}").text


def test_the_return_path_cannot_leave_the_site(client, shelf, printer):
    response = client.post("/labels/print", data={
        "_csrf": csrf(client, "/labels/choose"), "pick": f"i{shelf['a1'].id}",
        "next": "https://evil.example/",
    }, follow_redirects=False)
    assert response.headers["location"].startswith("/labels/choose")


def test_an_item_without_a_code_is_skipped_not_printed(client, shelf, printer):
    response = client.post("/labels/print", data={
        "_csrf": csrf(client, "/labels/choose"), "pick": f"i{shelf['nocode'].id}",
    }, follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert printer.jobs == []


# ---------------------------------------------------------------------------
# deployment
# ---------------------------------------------------------------------------


def _deploy(name):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "deploy" / name).read_text()


def test_the_service_can_open_a_usb_printer_and_nothing_else():
    unit = _deploy("stockroom.service")
    assert not re.search(r"^PrivateDevices=yes", unit, re.M)  # no printer in a private /dev
    assert re.search(r"^DevicePolicy=closed$", unit, re.M)
    allowed = re.findall(r"^DeviceAllow=(.+)$", unit, re.M)
    assert allowed == ["char-usb rw"]


def test_the_installer_puts_the_service_account_in_lp_and_names_the_printer():
    script = _deploy("setup-pi.sh")
    assert 'usermod -aG lp "$SERVICE_USER"' in script
    assert "99-stockroom-label.rules" in script
    assert "fonts-dejavu-core" in script


def test_the_udev_rule_makes_the_name_the_app_looks_for():
    rule = _deploy("99-stockroom-label.rules")
    symlink = re.search(r'SYMLINK\+="([^"]+)"', rule).group(1)
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "src" / "stockroom"
              / "config.py").read_text()
    default = re.search(r'"STOCKROOM_LABEL_PRINTER", "([^"]+)"', source).group(1)
    assert default == f"/dev/{symlink}"
