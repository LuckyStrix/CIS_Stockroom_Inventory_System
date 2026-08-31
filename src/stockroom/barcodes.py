"""Barcode rendering.

Codes themselves are allocated in :func:`stockroom.service.next_barcode`
(they need the database counter); this module only turns a code into
something printable.

Code128 is used because it encodes the full ASCII range -- our
``CIS-000142`` format contains letters and a hyphen, which EAN/UPC cannot
represent -- and every cheap USB scanner reads it out of the box.

SVG is generated rather than PNG so labels stay sharp at any printer DPI and
can be inlined straight into the label sheet with no image files on disk.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

import barcode
from barcode.writer import SVGWriter

# Tuned for a 2.625" x 1" label (Avery 5160). module_height is in mm.
_WRITER_OPTIONS = {
    "module_width": 0.28,
    "module_height": 11.0,
    "quiet_zone": 2.0,
    "font_size": 0,      # we draw the human-readable text ourselves
    "text_distance": 0,
    "write_text": False,
}

_SVG_TAG = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)

# python-barcode paints its bars with style="fill:black;". A strict CSP
# without 'unsafe-inline' blocks inline style attributes, which would leave
# every barcode on the page unpainted -- and silently, since nothing errors.
# Presentation attributes carry the same meaning and are not style, so they
# survive the policy. Covered by test_the_page_carries_no_inline_handlers.
_STYLE_FILL = re.compile(r'\sstyle="fill:\s*([#\w]+)\s*;?\s*"', re.IGNORECASE)


def render_svg(code: str) -> str:
    """Return an inline ``<svg>`` element encoding ``code`` as Code128.

    The XML declaration and DOCTYPE are stripped so the result can be dropped
    straight into an HTML template.

    The root element's ``width``/``height`` are deliberately left as the
    physical millimetre sizes python-barcode computes. A barcode has to come
    off the printer at a real size to scan reliably, so the label sheet sizes
    its cells around the barcode rather than scaling the barcode to the cell.
    """
    code = (code or "").strip()
    if not code:
        return ""

    svg_bytes = barcode.get("code128", code, writer=SVGWriter()).render(_WRITER_OPTIONS)
    svg = svg_bytes.decode("utf-8")

    # Drop everything before the root element (XML declaration, DOCTYPE).
    start = svg.find("<svg")
    if start == -1:  # pragma: no cover - defensive
        return ""
    svg = svg[start:]

    svg = _SVG_TAG.sub(
        lambda m: m.group(0).replace("<svg", '<svg class="barcode"', 1), svg, count=1
    )
    return _STYLE_FILL.sub(r' fill="\1"', svg)


def is_valid(code: str) -> bool:
    """Whether Code128 can encode this string (it covers ASCII 0-127)."""
    code = (code or "").strip()
    if not code:
        return False
    try:
        barcode.get("code128", code, writer=SVGWriter())
        return True
    except Exception:
        return False


def label_text(name: str, limit: int = 34) -> str:
    """Item name trimmed to fit one line on a label."""
    name = escape((name or "").strip())
    return name if len(name) <= limit else name[: limit - 1] + "…"
