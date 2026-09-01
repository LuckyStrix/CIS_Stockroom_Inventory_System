"""Counting the shelves, and reconciling what is there with what should be.

Everything else in this system records what people *told* it. The loan table
knows what went through the counter; it cannot know about the tripod somebody
put back on the wrong shelf, the card reader that walked out during an open
lab, or the box that was never entered in the first place. Drift of that kind
is invisible to every other page here, and it accumulates quietly for years.

A stocktake is the only thing that catches it. Walk the room with a scanner,
scan what is actually on the shelves, and compare.

    WHAT "EXPECTED" MEANS

    Expected on the shelf is `item_status.available` -- quantity, minus what
    is on loan, minus what is held out of service. It is derived at
    reconciliation time from the same view everything else reads, so a
    stocktake cannot form its own opinion about what should have been there.

A discrepancy is never resolved automatically. A shortfall becomes a `missing`
hold only when a human says so, because the overwhelmingly common cause of a
missing scan is a missed scan.

Same rule as everywhere else: every mutation writes its `event` row in the
same transaction as the change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db, search
from .models import Item
from .service import (
    Actor,
    ConflictError,
    NotFound,
    _clean,
    _optional,
    get_item,
    log_event,
)


@dataclass(frozen=True, slots=True)
class Stocktake:
    id: int
    started_at: str
    started_by: str
    scope_unit: str | None
    note: str
    status: str
    finished_at: str | None
    finished_by: str | None

    @classmethod
    def from_row(cls, row: Any) -> Stocktake:
        return cls(**{k: row[k] for k in row.keys() if k in cls.__slots__})

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def scope_label(self) -> str:
        return self.scope_unit or "the whole stockroom"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One item where the shelf and the database disagree."""

    item_id: int
    item_name: str
    barcode: str | None
    location: str
    expected: int
    counted: int

    @property
    def difference(self) -> int:
        """Positive means more on the shelf than expected; negative, fewer."""
        return self.counted - self.expected

    @property
    def is_short(self) -> bool:
        return self.counted < self.expected

    @property
    def label(self) -> str:
        if self.is_short:
            return f"{self.expected - self.counted} missing"
        return f"{self.counted - self.expected} more than expected"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What a stocktake found, once everything has been scanned."""

    stocktake: Stocktake
    matched: list[Discrepancy]
    short: list[Discrepancy]
    over: list[Discrepancy]
    unscanned: list[Discrepancy]

    @property
    def problems(self) -> list[Discrepancy]:
        """Everything a human needs to look at, worst first."""
        return sorted(
            self.short + self.unscanned + self.over,
            key=lambda d: (d.difference, d.item_name.lower()),
        )

    @property
    def counted_items(self) -> int:
        return len(self.matched) + len(self.short) + len(self.over)

    @property
    def is_clean(self) -> bool:
        return not self.problems


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_stocktake(conn: sqlite3.Connection, stocktake_id: int) -> Stocktake:
    row = conn.execute(
        "SELECT * FROM stocktake WHERE id = ?", (stocktake_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"No stocktake with id {stocktake_id}.")
    return Stocktake.from_row(row)


def open_stocktake(conn: sqlite3.Connection) -> Stocktake | None:
    """The one in progress, if there is one."""
    row = conn.execute(
        "SELECT * FROM stocktake WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Stocktake.from_row(row) if row else None


def list_stocktakes(conn: sqlite3.Connection, *, limit: int = 25) -> list[Stocktake]:
    rows = conn.execute(
        "SELECT * FROM stocktake ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    return [Stocktake.from_row(r) for r in rows]


def scan_counts(conn: sqlite3.Connection, stocktake_id: int) -> dict[int, int]:
    """Units counted per item in this session."""
    rows = conn.execute(
        "SELECT item_id, SUM(quantity) AS n FROM stocktake_scan "
        "WHERE stocktake_id = ? GROUP BY item_id",
        (stocktake_id,),
    )
    return {r["item_id"]: r["n"] for r in rows}


def reconcile(conn: sqlite3.Connection, stocktake_id: int) -> Reconciliation:
    """What this stocktake found.

    For a session still open this is computed live, against the shelves as
    they are right now -- which is what makes the progress view useful while
    somebody is walking the room.

    For a closed one it is read back from `stocktake_result`, exactly as it
    was recorded when the count was finished. That distinction is the point:
    a finished count is an observation of a particular day, and recomputing it
    later compared old scans against new stock, so March's report grew April's
    discrepancies every time something was lent out.
    """
    session = get_stocktake(conn, stocktake_id)
    if not session.is_open:
        frozen = _frozen_result(conn, session)
        if frozen is not None:
            return frozen
        # A session closed before this table existed, or abandoned (which
        # writes no result, because a half-count is not a finding). Fall
        # through and compute, which is the old behaviour and still the best
        # available answer.
    return _reconcile_live(conn, session)


def _frozen_result(
    conn: sqlite3.Connection, session: Stocktake
) -> Reconciliation | None:
    """Rebuild a finished stocktake's findings from the recorded rows."""
    rows = conn.execute(
        "SELECT * FROM stocktake_result WHERE stocktake_id = ? "
        "ORDER BY item_name COLLATE NOCASE",
        (session.id,),
    ).fetchall()
    if not rows:
        return None

    lists: dict[str, list[Discrepancy]] = {
        "matched": [], "short": [], "over": [], "unscanned": [],
    }
    for row in rows:
        lists[row["kind"]].append(Discrepancy(
            item_id=row["item_id"],
            item_name=row["item_name"],
            barcode=row["barcode"],
            location=row["location"],
            expected=row["expected"],
            counted=row["counted"],
        ))
    return Reconciliation(
        session, lists["matched"], lists["short"], lists["over"],
        lists["unscanned"],
    )


def _reconcile_live(
    conn: sqlite3.Connection, session: Stocktake
) -> Reconciliation:
    """Compare what was scanned against what is on the shelf right now."""
    counted = scan_counts(conn, session.id)

    sql = "SELECT * FROM item_status WHERE archived_at IS NULL"
    params: list[Any] = []
    if session.scope_unit:
        sql += " AND unit = ?"
        params.append(session.scope_unit)
    sql += " ORDER BY name COLLATE NOCASE"

    matched: list[Discrepancy] = []
    short: list[Discrepancy] = []
    over: list[Discrepancy] = []
    unscanned: list[Discrepancy] = []

    for row in conn.execute(sql, params).fetchall():
        # Straight from the row in hand. This used to re-fetch each item by id,
        # which is a second query per item across the whole scoped inventory --
        # and finish_stocktake runs the whole comparison inside the write
        # transaction, so on a full count that was thousands of needless
        # queries holding the database's only write lock.
        item = Item.from_row(row)
        found = counted.pop(item.id, None)
        entry = Discrepancy(
            item_id=item.id,
            item_name=item.name,
            barcode=item.barcode,
            location=item.location,
            expected=item.available,
            counted=found or 0,
        )
        if found is None:
            # Never scanned at all. Distinguished from "scanned and came up
            # short" because the likeliest explanation is a shelf nobody
            # walked, not stock that vanished.
            if item.available > 0:
                unscanned.append(entry)
            else:
                matched.append(entry)
        elif found == item.available:
            matched.append(entry)
        elif found < item.available:
            short.append(entry)
        else:
            over.append(entry)

    # Anything left in `counted` was scanned but is out of scope -- an item
    # from another storage unit sitting on these shelves. Worth reporting: it
    # is misfiled, which is its own kind of lost.
    for item_id, found in counted.items():
        item = get_item(conn, item_id)
        over.append(Discrepancy(
            item_id=item.id, item_name=item.name, barcode=item.barcode,
            location=item.location, expected=0, counted=found,
        ))

    return Reconciliation(session, matched, short, over, unscanned)


def _freeze_result(
    conn: sqlite3.Connection, stocktake_id: int, result: Reconciliation
) -> None:
    """Record what the count found, so the report stops moving.

    Called only from finish_stocktake, inside the transaction that closes the
    session. Nothing ever updates these rows: they are the observation, not a
    cache of one.
    """
    for kind, entries in (
        ("matched", result.matched),
        ("short", result.short),
        ("over", result.over),
        ("unscanned", result.unscanned),
    ):
        for entry in entries:
            conn.execute(
                """
                INSERT INTO stocktake_result
                    (stocktake_id, item_id, item_name, barcode, location,
                     expected, counted, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (stocktake_id, entry.item_id, entry.item_name, entry.barcode,
                 entry.location, entry.expected, entry.counted, kind),
            )


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def start_stocktake(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    scope_unit: str | None = None,
    note: str = "",
) -> Stocktake:
    """Begin counting. One at a time, stockroom-wide or one storage unit."""
    with db.transaction(conn):
        existing = open_stocktake(conn)
        if existing is not None:
            raise ConflictError(
                f"A stocktake of {existing.scope_label} is already in progress "
                f"(started {existing.started_at}). Finish or abandon it first."
            )
        cur = conn.execute(
            "INSERT INTO stocktake (started_at, started_by, scope_unit, note) "
            "VALUES (?, ?, ?, ?)",
            (db.utcnow(), str(actor), _optional(scope_unit), _clean(note)),
        )
        stocktake_id = int(cur.lastrowid)
        session = get_stocktake(conn, stocktake_id)
        log_event(
            conn,
            actor=actor,
            action="stocktake.start",
            entity_type="stocktake",
            entity_id=stocktake_id,
            summary=f"Started a stocktake of {session.scope_label}",
        )
        return session


def record_scan(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    stocktake_id: int,
    code: str = "",
    item_id: int | None = None,
    unit_id: int | None = None,
    quantity: int = 1,
) -> tuple[Stocktake, str]:
    """Record one thing seen on the shelf. Returns the session and a message.

    Accepts a scanned code -- an item barcode or a unit's asset tag -- or an
    explicit item. Scanning the same thing again adds to its count rather than
    creating a second row, so walking a shelf and scanning six SD card boxes
    reads as six, not as six conflicting rows saying one.
    """
    with db.transaction(conn):
        session = get_stocktake(conn, stocktake_id)
        if not session.is_open:
            raise ConflictError("That stocktake is closed.")

        if item_id is None:
            code = _clean(code)
            if not code:
                raise ConflictError("Nothing was scanned.")
            # search.resolve understands asset tags now. This used to do its
            # own unit_status lookup here, which is why the counter and the
            # CLI could not scan an asset tag while the stocktake could.
            found = search.resolve(conn, code)
            if found is None:
                raise NotFound(
                    f"Nothing in the stockroom matches {code!r}. It may be "
                    "something that was never entered -- add it, then scan "
                    "it again."
                )
            unit = found.unit
            item_id = found.item.id
            if unit is not None:
                unit_id, quantity = unit.id, 1

        item = get_item(conn, item_id)
        conn.execute(
            """
            INSERT INTO stocktake_scan (stocktake_id, item_id, unit_id,
                                        quantity, scanned_at, scanned_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (stocktake_id, item_id, unit_id) DO UPDATE
                SET quantity = quantity + excluded.quantity,
                    scanned_at = excluded.scanned_at
            """,
            (stocktake_id, item_id, unit_id, quantity, db.utcnow(), str(actor)),
        )
        total = scan_counts(conn, stocktake_id).get(item_id, quantity)

        # Deliberately no event per scan. A stocktake of a thousand items would
        # write a thousand rows into the history and bury the inventory record
        # it exists to protect -- the same reasoning as the session heartbeat
        # exception in CLAUDE.md. The stocktake itself is audited at start and
        # finish, and its scans are all in stocktake_scan.
        return session, f"{item.name}: {total} counted"


def finish_stocktake(
    conn: sqlite3.Connection, *, actor: Actor, stocktake_id: int
) -> Reconciliation:
    """Close the count and record what it found."""
    with db.transaction(conn):
        session = get_stocktake(conn, stocktake_id)
        if not session.is_open:
            raise ConflictError("That stocktake is already closed.")

        now = db.utcnow()
        # Reconcile BEFORE the status changes, so this reads the live
        # comparison rather than the frozen rows it is about to write.
        result = _reconcile_live(conn, session)
        conn.execute(
            "UPDATE stocktake SET status = 'finished', finished_at = ?, "
            "finished_by = ? WHERE id = ?",
            (now, str(actor), stocktake_id),
        )
        _freeze_result(conn, stocktake_id, result)
        summary = (
            f"Finished the stocktake of {session.scope_label}: "
            f"{result.counted_items} item(s) counted, "
            f"{len(result.problems)} discrepancy(ies)"
        )
        log_event(
            conn,
            actor=actor,
            action="stocktake.finish",
            entity_type="stocktake",
            entity_id=stocktake_id,
            summary=summary,
            changes={
                "short": {"from": None, "to": [d.item_name for d in result.short]},
                "unscanned": {
                    "from": None, "to": [d.item_name for d in result.unscanned]
                },
                "over": {"from": None, "to": [d.item_name for d in result.over]},
            },
        )
        # The same object that was just recorded, not a second full
        # reconciliation of the whole inventory inside the write transaction.
        return _frozen_result(conn, get_stocktake(conn, stocktake_id)) or result


def abandon_stocktake(
    conn: sqlite3.Connection, *, actor: Actor, stocktake_id: int, reason: str = ""
) -> Stocktake:
    """Give up on a count. Its scans stay, so a half-count is still a record."""
    with db.transaction(conn):
        session = get_stocktake(conn, stocktake_id)
        if not session.is_open:
            raise ConflictError("That stocktake is already closed.")
        now = db.utcnow()
        conn.execute(
            "UPDATE stocktake SET status = 'abandoned', finished_at = ?, "
            "finished_by = ? WHERE id = ?",
            (now, str(actor), stocktake_id),
        )
        summary = f"Abandoned the stocktake of {session.scope_label}"
        if _clean(reason):
            summary += f" ({_clean(reason)})"
        log_event(
            conn,
            actor=actor,
            action="stocktake.abandon",
            entity_type="stocktake",
            entity_id=stocktake_id,
            summary=summary,
        )
        return get_stocktake(conn, stocktake_id)
