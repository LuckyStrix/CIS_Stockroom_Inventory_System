"""Barcode labels on a Brother QL label printer plugged into the Pi.

The label sheet at /labels is for an office printer and a browser. This is the
other route: a thermal label printer on the Pi's own USB port, and a "Print
label" button that works from whatever phone or laptop staff happen to have in
their hand -- the printer is attached to the server, not to them.

There is no driver and no CUPS. Brother's own Linux driver is an i386 binary
that does not exist for a Pi, and the printer does not need one: the kernel's
``usblp`` driver exposes it as a character device, and the QL raster protocol
is a handful of escape sequences around one uncompressed bitmap per label.
Everything here is Pillow and python-barcode, which the application already
depends on.

Developed against a QL-500, which is the oldest of the family: no compression,
no mode switching, no cutter. Those are the commands it is given, and the
later QL models accept the same subset.

The printer is asked what tape is loaded before every job and the label is
laid out for it, so swapping a 62 mm roll for 29 mm die-cut labels needs no
setting. ``stockroom label status`` says what it sees.

Printing writes nothing to the database, so it is not an audited mutation
under the rule in CLAUDE.md -- a label is a rendering of a code that already
exists. Who printed what is logged to the journal.
"""

from __future__ import annotations

import fcntl
import logging
import os
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import barcodes, config

log = logging.getLogger(__name__)

DPI = 300
# The QL-500's head is 720 dots wide whatever tape is in it: 90 bytes a line.
_HEAD_DOTS = 720
_ROW_BYTES = _HEAD_DOTS // 8

# Status reply, byte 18.
_REPLY, _PRINTED, _ERROR, _PHASE = 0x00, 0x01, 0x02, 0x06

# Brother's error bits, status bytes 8 and 9, in the words someone standing at
# the printer would use.
_ERRORS_1 = {
    0x01: "no tape is loaded",
    0x02: "the tape has run out",
    0x04: "the cutter is jammed",
    0x10: "the printer is busy",
    0x20: "the printer is switched off",
    0x80: "fan error",
}
_ERRORS_2 = {
    0x01: "the wrong tape is loaded",
    0x04: "transmission error",
    0x10: "the cover is open",
    0x40: "the tape will not feed",
    0x80: "system error",
}

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


class LabelPrinterError(Exception):
    """Something a person can fix: no printer, no tape, cover open."""


@dataclass(frozen=True, slots=True)
class Tape:
    """One kind of Brother DK tape, in print-head dots at 300 dpi.

    ``across`` is the printable width; ``along`` the printable length of a
    die-cut label, or 0 for continuous tape, where the label is as long as we
    make it. ``margin`` is how many dots of the head sit unused to the right
    of the tape. The figures are Brother's, by way of the brother_ql project.
    """

    width_mm: int
    length_mm: int
    across: int
    along: int
    margin: int

    @property
    def continuous(self) -> bool:
        return self.length_mm == 0

    @property
    def name(self) -> str:
        if self.continuous:
            return f"{self.width_mm} mm continuous"
        return f"{self.width_mm} × {self.length_mm} mm die-cut"


# Only tapes big enough for a name, a location and a barcode a scanner will
# read. 12 mm and the small die-cuts are left out deliberately: a barcode
# squeezed onto them comes off the printer and does not scan, which is worse
# than refusing.
TAPES = {
    (t.width_mm, t.length_mm): t
    for t in (
        Tape(29, 0, 306, 0, 6),
        Tape(38, 0, 413, 0, 12),
        Tape(50, 0, 554, 0, 12),
        Tape(54, 0, 590, 0, 0),
        Tape(62, 0, 696, 0, 12),
        Tape(29, 90, 306, 991, 6),
        Tape(38, 90, 413, 991, 12),
        Tape(39, 48, 425, 495, 6),
        Tape(52, 29, 578, 271, 0),
        Tape(62, 29, 696, 271, 12),
        Tape(62, 100, 696, 1109, 12),
    )
}

# On narrow continuous tape the label is printed sideways along the roll, and
# this is how much roll it takes -- the same 59 mm as the 62 mm tape's width.
_SIDEWAYS_LENGTH = 696


@dataclass(frozen=True, slots=True)
class Label:
    code: str
    title: str
    subtitle: str = ""


@dataclass(frozen=True, slots=True)
class Status:
    width_mm: int
    length_mm: int
    errors: list[str] = field(default_factory=list)

    @property
    def tape(self) -> Tape | None:
        return TAPES.get((self.width_mm, self.length_mm))

    def describe_tape(self) -> str:
        if not self.width_mm:
            return "no tape"
        if self.tape is not None:
            return self.tape.name
        kind = "continuous" if not self.length_mm else f"× {self.length_mm} mm die-cut"
        return f"{self.width_mm} mm {kind}"

    def usable_tape(self) -> Tape:
        """The loaded tape, or a LabelPrinterError saying why it will not do."""
        if self.errors:
            raise LabelPrinterError("The label printer says " + "; ".join(self.errors) + ".")
        if self.tape is None:
            raise LabelPrinterError(
                f"The label printer has {self.describe_tape()} tape loaded, which is "
                f"too small for a stockroom label. Load 29 mm or wider."
            )
        return self.tape


def _parse_status(reply: bytes) -> Status:
    errors = [msg for bit, msg in _ERRORS_1.items() if reply[8] & bit]
    errors += [msg for bit, msg in _ERRORS_2.items() if reply[9] & bit]
    return Status(width_mm=reply[10], length_mm=reply[17], errors=errors)


# ---------------------------------------------------------------------------
# the device
# ---------------------------------------------------------------------------


def device_path() -> Path | None:
    """Where the printer is configured to be, or None if printing is off."""
    raw = config.LABEL_PRINTER
    return Path(raw) if raw else None


def connected() -> bool:
    """Whether a printer is plugged in right now. Cheap: one stat()."""
    path = device_path()
    return path is not None and path.exists()


class _Device:
    """The open character device, held under an exclusive lock.

    The lock is flock() on the device itself, so it serialises the web
    service, `stockroom label` and `stockroom doctor` alike. A status request
    begins by flushing the printer's input, which would wreck a job another
    process was half-way through sending.
    """

    def __init__(self, path: Path, *, wait: float):
        try:
            self.fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            raise LabelPrinterError("The label printer is not plugged in.") from None
        except PermissionError:
            # Two different locks answer with the same EPERM: the file mode
            # (root:lp 0660) and the service unit's device policy. Naming only
            # the first sent the reader to `id stockroom`, which looks fine,
            # when what was missing was the unit's DeviceAllow line.
            raise LabelPrinterError(
                f"No permission to use the label printer at {path}. The service "
                f"account must be in the lp group, and stockroom.service must "
                f"allow USB printers (DeviceAllow=char-usb rw); "
                f"deploy/setup-pi.sh sets up both."
            ) from None
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise LabelPrinterError(
                        "The label printer is busy with another job. "
                        "Try again when it stops."
                    ) from None
                time.sleep(0.1)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def read_status(self, timeout: float) -> bytes | None:
        """One 32-byte status packet, or None if the printer said nothing."""
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(self.fd, 32 - len(buf))
                if chunk:
                    buf += chunk
        return buf

    def close(self) -> None:
        os.close(self.fd)  # releases the flock with it


# Indirection so the tests can stand a fake printer in for the device.
_connect = _Device

# Clears any half-received job out of the printer, then resets it. Brother's
# documented preamble; without it a job abandoned mid-transfer leaves the
# printer reading the next job's header as bitmap.
_RESET = b"\x00" * 200 + b"\x1b@"


def _query(dev) -> Status:
    dev.write(_RESET + b"\x1biS")
    reply = dev.read_status(timeout=3)
    if reply is None:
        raise LabelPrinterError("The label printer did not answer. Is it switched on?")
    return _parse_status(reply)


def status() -> Status:
    """Ask the printer what it has loaded and whether anything is wrong."""
    path = device_path()
    if path is None:
        raise LabelPrinterError("Label printing is switched off (STOCKROOM_LABEL_PRINTER).")
    dev = _connect(path, wait=2)
    try:
        return _query(dev)
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(str(_FONT_DIR / name), size)
    except OSError:
        # fonts-dejavu-core is in setup-pi.sh; this keeps a machine without it
        # printing legible, if plainer, labels instead of none.
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _modules(code: str) -> str:
    """The Code128 bar pattern, one character per module, "1" for a bar."""
    import barcode

    return barcode.get("code128", code).build()[0]


# Sizes in dots at 300 dpi, not fractions of the label, so a label reads the
# same on every tape and a bigger label just has more white round it.
_PAD = 10
_TITLE, _SUBTITLE, _CODE = 36, 28, 32
# 8 mm. Code128 needs height only for a scanner held at a slant, and a
# hand-held scanner pointed at a bin needs about a quarter of the bar width;
# this is half of what the first version drew and still twice what it takes.
_BAR_HEIGHT = 94
_MIN_BAR_HEIGHT = 50


def _draw(label: Label, width: int, height: int | None = None) -> Image.Image:
    """Lay one label out on a landscape canvas, width >= height.

    ``height`` None means "as tall as the content", which is what continuous
    tape gets: the label is only as long as it needs to be. A fixed height
    (a die-cut label, or the width of a narrow roll) centres the content.
    """
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    inner = width - 2 * _PAD

    # Every bar is a whole number of dots, so the head never has to split a
    # dot between bar and space. 2 dots (0.17 mm) is the narrowest a cheap
    # scanner reads reliably; 4 is plenty and the widest worth spending tape
    # on. Ten modules of quiet zone each side is what Code128 asks for.
    modules = _modules(label.code)
    fits = [m for m in (4, 3, 2) if (len(modules) + 20) * m <= inner]
    if not fits:
        raise LabelPrinterError(
            f"{label.code} is too long to print as a barcode that will scan "
            f"on this tape. Use wider tape."
        )
    module = fits[0]

    title = _font("DejaVuSans-Bold.ttf", _TITLE)
    small = _font("DejaVuSans.ttf", _SUBTITLE)
    mono = _font("DejaVuSansMono-Bold.ttf", _CODE)
    title_text = _fit(probe, label.title, title, inner)
    subtitle_text = _fit(probe, label.subtitle, small, inner) if label.subtitle else ""

    text_rows = _TITLE + 6 + (_SUBTITLE + 6 if subtitle_text else 0)
    fixed = 2 * _PAD + text_rows + 6 + 6 + _CODE
    bars = _BAR_HEIGHT
    if height is not None:
        bars = min(bars, height - fixed)
        if bars < _MIN_BAR_HEIGHT:
            raise LabelPrinterError("This tape is too small for a stockroom label.")
    natural = fixed + bars
    height = natural if height is None else height

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    y = _PAD + (height - natural) // 2
    draw.text((width // 2, y), title_text, font=title, fill=0, anchor="mt")
    y += _TITLE + 6
    if subtitle_text:
        draw.text((width // 2, y), subtitle_text, font=small, fill=0, anchor="mt")
        y += _SUBTITLE + 6
    y += 6
    left = (width - len(modules) * module) // 2
    for i, bit in enumerate(modules):
        if bit == "1":
            x = left + i * module
            draw.rectangle([x, y, x + module - 1, y + bars - 1], fill=0)
    y += bars + 6
    draw.text((width // 2, y), label.code, font=mono, fill=0, anchor="mt")
    return image


def render(label: Label, tape: Tape) -> Image.Image:
    """The label as the printer will lay it down: ``tape.across`` dots wide.

    Always drawn landscape and turned to run along the tape when the tape is
    the narrow way round -- a 29 mm roll gets the same label as a 62 mm one,
    sideways, rather than a barcode squeezed to fit across it.
    """
    code = (label.code or "").strip()
    if not barcodes.is_valid(code):
        raise LabelPrinterError(f"{code!r} cannot be printed as a barcode.")
    label = Label(code, label.title.strip(), label.subtitle.strip())

    if tape.continuous:
        if tape.across >= 554:
            return _draw(label, tape.across)
        return _draw(label, _SIDEWAYS_LENGTH, tape.across).rotate(90, expand=True)
    if tape.across >= tape.along:
        return _draw(label, tape.across, tape.along)
    return _draw(label, tape.along, tape.across).rotate(90, expand=True)


# ---------------------------------------------------------------------------
# the raster protocol
# ---------------------------------------------------------------------------


def encode(images: list[Image.Image], tape: Tape) -> bytes:
    """A complete print job: one page per image, in Brother's QL raster format.

    Each image must be ``tape.across`` dots wide (what render() returns).
    """
    job = bytearray(_RESET)
    for page, image in enumerate(images):
        if image.width != tape.across:
            raise ValueError(f"image is {image.width} dots wide, tape takes {tape.across}")
        lines = image.height
        # Print information: valid-field flags, media type (0x0A continuous,
        # 0x0B die-cut), width and length in mm, raster line count, and
        # whether this is the first page of the job.
        job += b"\x1biz" + bytes([
            0x8E, 0x0A if tape.continuous else 0x0B, tape.width_mm, tape.length_mm,
        ]) + struct.pack("<L", lines) + bytes([0 if page == 0 else 1, 0])
        # Feed margin. Continuous tape needs one or labels butt up against
        # each other; a die-cut label is already positioned by its gap.
        job += b"\x1bid" + struct.pack("<H", 35 if tape.continuous else 0)

        head = Image.new("L", (_HEAD_DOTS, lines), 255)
        head.paste(image.convert("L"), (_HEAD_DOTS - tape.across - tape.margin, 0))
        # The head lays dots down mirrored relative to the bitmap, and a set
        # bit is a black dot -- the opposite of Pillow's "1" mode.
        bits = ImageOps.invert(ImageOps.mirror(head)).point(
            lambda v: 255 if v >= 128 else 0, mode="1"
        ).tobytes()
        for row in range(lines):
            job += b"g\x00" + bytes([_ROW_BYTES])
            job += bits[row * _ROW_BYTES:(row + 1) * _ROW_BYTES]

        # Form feed between pages, Ctrl-Z after the last.
        job += b"\x1a" if page == len(images) - 1 else b"\x0c"
    return bytes(job)


def _send(dev, job: bytes, pages: int) -> None:
    """Write a job and wait until the printer says every page is done."""
    dev.write(job)
    done = 0
    # The QL-500 does about a label a second; the floor covers the feed.
    deadline = time.monotonic() + 10 + 3 * pages
    while done < pages:
        remaining = deadline - time.monotonic()
        reply = dev.read_status(timeout=remaining) if remaining > 0 else None
        if reply is None:
            raise LabelPrinterError(
                f"The label printer stopped answering after {done} of {pages} label(s)."
            )
        if reply[18] == _PRINTED:
            done += 1
        elif reply[18] == _ERROR:
            problem = "; ".join(_parse_status(reply).errors) or "an error"
            raise LabelPrinterError(
                f"The label printer reported {problem} after {done} of {pages} label(s)."
            )


def print_labels(labels: list[Label], *, by: str = "") -> int:
    """Print now and return when the printer says it is done.

    For the CLI. The web routes use submit(), which does not hold a request
    open while a long strip of labels comes out.
    """
    dev, tape, images = _prepare(labels)
    try:
        _send(dev, encode(images, tape), len(images))
    finally:
        dev.close()
    log.info("printed %d label(s) on %s%s", len(images), tape.name, f" for {by}" if by else "")
    return len(images)


def submit(labels: list[Label], *, by: str = "") -> threading.Thread:
    """Check the printer, then print in the background.

    Everything a person can fix -- no printer, no tape, cover open, a code
    too long for the tape -- is found here, before this returns, so the page
    can say so. Only the printing itself is in the thread, and a failure part
    way through is logged rather than lost.
    """
    dev, tape, images = _prepare(labels)

    def run() -> None:
        try:
            _send(dev, encode(images, tape), len(images))
            log.info("printed %d label(s) on %s%s", len(images), tape.name,
                     f" for {by}" if by else "")
        except Exception:
            log.exception("label printing failed (%d label(s)%s)", len(images),
                          f" for {by}" if by else "")
        finally:
            dev.close()

    thread = threading.Thread(target=run, name="label-printer", daemon=True)
    thread.start()
    return thread


def _prepare(labels: list[Label]):
    """Open and lock the printer, check it, and render every label.

    Returns the device still open and locked, so nothing can slip a job in
    between the check and the print.
    """
    if not labels:
        raise LabelPrinterError("Nothing to print.")
    path = device_path()
    if path is None:
        raise LabelPrinterError("Label printing is switched off (STOCKROOM_LABEL_PRINTER).")
    dev = _connect(path, wait=2)
    try:
        tape = _query(dev).usable_tape()
        images = [render(label, tape) for label in labels]
    except BaseException:
        dev.close()
        raise
    return dev, tape, images


# ---------------------------------------------------------------------------
# what goes on a label
# ---------------------------------------------------------------------------


def for_item(item) -> Label:
    return Label(item.barcode or "", item.name, item.location)


def for_unit(unit, item) -> Label:
    """A unit's own label: its asset tag, which the scan box resolves to it."""
    parts = [f"S/N {unit.serial}" if unit.serial else "", item.location]
    return Label(unit.asset_tag or "", item.name, " · ".join(p for p in parts if p))
