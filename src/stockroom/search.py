"""Item search.

Two strategies, chosen per query:

* **FTS5** when the index exists -- prefix-matched, so typing "can" finds
  "Canon", which is what you want from a search-as-you-type box.
* **LIKE** otherwise, or when the FTS query is malformed. Slower, but the
  stockroom is on the order of thousands of items, not millions, so a table
  scan is still instant.

A query that looks like a barcode is resolved directly against the barcode
column first -- scanning a label should jump straight to that item rather
than ranking it among text matches. A unit's asset tag resolves the same way,
to the item plus the individual object it names.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import db
from .models import Item, Unit
from .service import get_item, get_item_by_barcode, get_unit_by_asset_tag

# Characters FTS5 treats as syntax. Stripping them keeps a user pasting
# something like "Canon EOS-R5 (body)" from producing a syntax error.
_FTS_SPECIAL = re.compile(r'["\'\*\(\):^\-]')


def _fts_query(text: str) -> str:
    """Turn user text into a safe prefix-matching FTS5 query."""
    tokens = [t for t in _FTS_SPECIAL.sub(" ", text).split() if t]
    return " AND ".join(f'"{t}"*' for t in tokens)


def looks_like_barcode(text: str) -> bool:
    """Heuristic: our own CIS-000123 codes, or a bare run of >=6 digits."""
    text = text.strip()
    return bool(
        re.fullmatch(r"[A-Za-z]{2,5}-\d{3,}", text) or re.fullmatch(r"\d{6,}", text)
    )


def search_items(
    conn: sqlite3.Connection,
    query: str,
    *,
    include_archived: bool = False,
    limit: int = 100,
) -> list[Item]:
    """Search items by name, description, barcode or location."""
    query = (query or "").strip()
    if not query:
        return []

    archived_clause = "" if include_archived else " AND s.archived_at IS NULL"

    if db.fts_enabled(conn):
        match = _fts_query(query)
        if match:
            try:
                rows = conn.execute(
                    f"""
                    SELECT s.* FROM item_fts f
                    JOIN item_status s ON s.id = f.rowid
                    WHERE item_fts MATCH ?{archived_clause}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
                return [Item.from_row(r) for r in rows]
            except sqlite3.OperationalError:
                # Malformed MATCH expression -- fall through to LIKE.
                pass

    like = f"%{query}%"
    rows = conn.execute(
        f"""
        SELECT s.* FROM item_status s
        WHERE (s.name LIKE ? OR s.description LIKE ? OR s.barcode LIKE ?
               OR s.unit LIKE ? OR s.shelf LIKE ?
               OR COALESCE(s.sub_location, '') LIKE ?){archived_clause}
        ORDER BY s.name COLLATE NOCASE
        LIMIT ?
        """,
        (like, like, like, like, like, like, limit),
    ).fetchall()
    return [Item.from_row(r) for r in rows]


@dataclass(frozen=True, slots=True)
class Scan:
    """What a scanned code turned out to be.

    ``unit`` is set only when an asset tag was scanned, in which case ``item``
    is the kind of thing it is. Scanning an item barcode tells you what kind
    of thing it is; scanning an asset tag tells you which one -- and until
    this existed, only the stocktake could tell the difference.
    """

    item: Item
    unit: Unit | None = None

    @property
    def names_a_unit(self) -> bool:
        return self.unit is not None


def resolve(conn: sqlite3.Connection, code: str) -> Scan | None:
    """Resolve a scanned or typed code to exactly one thing, or None.

    In order: an item barcode, a unit's asset tag, then a text search that
    yields a result only if it is unambiguous -- the scan box is also the
    search box, and jumping to the wrong item is worse than showing a list.

    Asset tags come second rather than first only because item barcodes are
    what most scans are. The two namespaces are separate, so the order is a
    matter of cost, not correctness.
    """
    code = (code or "").strip()
    if not code:
        return None

    item = get_item_by_barcode(conn, code)
    if item is not None:
        return Scan(item)

    unit = get_unit_by_asset_tag(conn, code)
    if unit is not None:
        return Scan(get_item(conn, unit.item_id), unit)

    matches = search_items(conn, code, limit=2)
    return Scan(matches[0]) if len(matches) == 1 else None


def resolve_scan(conn: sqlite3.Connection, code: str) -> Item | None:
    """Resolve a scanned code to an item, ignoring which unit it was.

    Kept for callers that only ever act on the item -- the CLI's item token,
    for one. Anything that could usefully know *which* camera was scanned
    should call :func:`resolve` instead.
    """
    found = resolve(conn, code)
    return found.item if found else None
