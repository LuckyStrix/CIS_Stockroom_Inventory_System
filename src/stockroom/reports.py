"""What the audit log can tell you once there is a year of it.

Every number here is a single query over data the system already keeps. None
of it is new bookkeeping -- it is the payoff for having recorded everything in
the first place.

The audience matters. "Which items has nobody borrowed in a year" and "what
did we spend the most time lending" are the two questions that decide what the
stockroom buys next and what it should stop storing, and they are the ones a
department asks before releasing money. A stockroom that can answer them from
its own records is in a much better position than one that cannot.

Everything here is a **read**. There are no mutations in this module, so it
needs no audit events and takes no actor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

# A year is the natural window for a university stockroom: it covers both
# semesters and the summer, so "never borrowed" means never borrowed by
# anybody currently here.
DEFAULT_WINDOW_DAYS = 365


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass(frozen=True, slots=True)
class Row:
    """One line of a report: a label, a number, and something to link to."""

    label: str
    value: float
    detail: str = ""
    item_id: int | None = None
    person_id: int | None = None

    @property
    def display(self) -> str:
        if self.value == int(self.value):
            return str(int(self.value))
        return f"{self.value:.1f}"


# ---------------------------------------------------------------------------
# what gets borrowed
# ---------------------------------------------------------------------------


def most_borrowed(
    conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 15
) -> list[Row]:
    """The items that leave the room most often.

    Counted in loans, not units: twenty SD cards on one loan is one trip to
    the counter, and it is trips that tell you what the stockroom is for.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.name, COUNT(*) AS loans, SUM(l.quantity) AS units
        FROM loan l
        JOIN item i ON i.id = l.item_id
        WHERE l.checked_out_at >= ? AND l.split_from_loan_id IS NULL
        GROUP BY i.id
        ORDER BY loans DESC, units DESC
        LIMIT ?
        """,
        (_cutoff(days), int(limit)),
    )
    return [
        Row(r["name"], r["loans"], f"{r['units']} unit(s)", item_id=r["id"])
        for r in rows
    ]


def never_borrowed(
    conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS
) -> list[Row]:
    """Items nobody has taken out in the window. Deaccession candidates.

    Shelf space is the scarcest thing in a stockroom. This is the list to walk
    before buying more storage -- though read it with judgement: a light meter
    borrowed once every three years may still be the reason the stockroom
    exists.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.name, i.quantity, i.unit, i.shelf,
               MAX(l.checked_out_at) AS last_out
        FROM item i
        LEFT JOIN loan l ON l.item_id = i.id
        WHERE i.archived_at IS NULL
        GROUP BY i.id
        HAVING last_out IS NULL OR last_out < ?
        ORDER BY (last_out IS NOT NULL), last_out, i.name COLLATE NOCASE
        """,
        (_cutoff(days),),
    )
    return [
        Row(
            r["name"],
            r["quantity"],
            "never borrowed" if r["last_out"] is None
            else f"last out {r['last_out'][:10]}",
            item_id=r["id"],
        )
        for r in rows
    ]


def loan_durations(
    conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 15
) -> list[Row]:
    """Median days out, per item.

    Median rather than mean: one camera that spent a semester on a research
    project would drag an average into meaninglessness, and the useful
    question is what a typical loan of this thing looks like.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.name,
               julianday(REPLACE(l.returned_at, 'Z', ''))
             - julianday(REPLACE(l.checked_out_at, 'Z', '')) AS days_out
        FROM loan l
        JOIN item i ON i.id = l.item_id
        WHERE l.returned_at IS NOT NULL AND l.checked_out_at >= ?
        """,
        (_cutoff(days),),
    ).fetchall()

    by_item: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = by_item.setdefault(row["id"], {"name": row["name"], "days": []})
        if row["days_out"] is not None:
            entry["days"].append(row["days_out"])

    out: list[Row] = []
    for item_id, entry in by_item.items():
        if not entry["days"]:
            continue
        ordered = sorted(entry["days"])
        middle = len(ordered) // 2
        median = (
            ordered[middle] if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2
        )
        out.append(Row(entry["name"], round(median, 1),
                       f"{len(ordered)} loan(s)", item_id=item_id))
    out.sort(key=lambda r: -r.value)
    return out[:limit]


def busiest_weeks(
    conn: sqlite3.Connection, *, weeks: int = 26
) -> list[Row]:
    """Checkouts per week, oldest first. Shows the shape of the semester."""
    rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', REPLACE(checked_out_at, 'Z', '')) AS week,
               COUNT(*) AS loans
        FROM loan
        WHERE checked_out_at >= ? AND split_from_loan_id IS NULL
        GROUP BY week
        ORDER BY week
        """,
        (_cutoff(weeks * 7),),
    )
    return [Row(r["week"], r["loans"]) for r in rows]


# ---------------------------------------------------------------------------
# who borrows, and who brings things back
# ---------------------------------------------------------------------------


def busiest_borrowers(
    conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 15
) -> list[Row]:
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.email, COUNT(*) AS loans
        FROM loan l
        JOIN person p ON p.id = l.person_id
        WHERE l.checked_out_at >= ? AND l.split_from_loan_id IS NULL
        GROUP BY p.id
        ORDER BY loans DESC
        LIMIT ?
        """,
        (_cutoff(days), int(limit)),
    )
    return [Row(r["name"], r["loans"], r["email"], person_id=r["id"]) for r in rows]


def overdue_offenders(
    conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 15
) -> list[Row]:
    """Who returns things late, as a share of what they borrowed.

    A count alone would just name whoever borrows most. This is meant to find
    the person who needs a word, not to rank the stockroom's best customers.
    Only counts people with at least three loans, because one late return out
    of one is not a pattern.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.name,
               COUNT(*) AS loans,
               SUM(CASE
                   WHEN l.due_at IS NOT NULL
                    AND COALESCE(l.returned_at, ?) > l.due_at
                   THEN 1 ELSE 0 END) AS late
        FROM loan l
        JOIN person p ON p.id = l.person_id
        WHERE l.checked_out_at >= ? AND l.due_at IS NOT NULL
        GROUP BY p.id
        HAVING loans >= 3 AND late > 0
        ORDER BY (CAST(late AS REAL) / loans) DESC, late DESC
        LIMIT ?
        """,
        (db.utcnow(), _cutoff(days), int(limit)),
    )
    return [
        Row(r["name"], round(100.0 * r["late"] / r["loans"]),
            f"{r['late']} late of {r['loans']}", person_id=r["id"])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# what the stockroom has lost
# ---------------------------------------------------------------------------


def unaccounted(conn: sqlite3.Connection) -> list[Row]:
    """Units nobody can find, by item. The list that justifies a budget."""
    rows = conn.execute(
        """
        SELECT i.id, i.name, SUM(h.quantity) AS units,
               GROUP_CONCAT(DISTINCT h.state) AS states
        FROM item_hold h
        JOIN item i ON i.id = h.item_id
        WHERE h.closed_at IS NULL AND h.state IN ('missing', 'gone')
        GROUP BY i.id
        ORDER BY units DESC, i.name COLLATE NOCASE
        """
    )
    return [
        Row(r["name"], r["units"], r["states"].replace(",", ", "), item_id=r["id"])
        for r in rows
    ]


def out_of_service(conn: sqlite3.Connection) -> list[Row]:
    """Units broken or in repair -- the queue of things somebody should chase."""
    rows = conn.execute(
        """
        SELECT i.id, i.name, SUM(h.quantity) AS units,
               MIN(h.opened_at) AS since
        FROM item_hold h
        JOIN item i ON i.id = h.item_id
        WHERE h.closed_at IS NULL AND h.state IN ('broken', 'repair')
        GROUP BY i.id
        ORDER BY since
        """
    )
    return [
        Row(r["name"], r["units"], f"since {r['since'][:10]}", item_id=r["id"])
        for r in rows
    ]


def headline(conn: sqlite3.Connection, *, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """The numbers that go at the top of the page."""
    since = _cutoff(days)
    loans = conn.execute(
        "SELECT COUNT(*) AS n FROM loan WHERE checked_out_at >= ? "
        "AND split_from_loan_id IS NULL",
        (since,),
    ).fetchone()["n"]
    borrowers = conn.execute(
        "SELECT COUNT(DISTINCT person_id) AS n FROM loan WHERE checked_out_at >= ?",
        (since,),
    ).fetchone()["n"]
    units = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS n FROM loan WHERE checked_out_at >= ?",
        (since,),
    ).fetchone()["n"]
    return {
        "window_days": days,
        "loans": loans,
        "borrowers": borrowers,
        "units": units,
    }


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------


def bar_chart(rows: list[Row], *, width: int = 520, label_width: int = 170) -> str:
    """A horizontal bar chart as inline SVG.

    Server-rendered, with `width=` and `fill=` **presentation attributes**
    rather than CSS. That is not a stylistic choice: the CSP has no
    'unsafe-inline', so a `style=` attribute is dropped silently and the chart
    would render as a row of invisible bars with no error anywhere. The same
    constraint is why barcodes.py rewrites python-barcode's inline fills, and
    it is also why there is no charting library here -- every one of them
    styles inline, and no external script may load at all.

    Colours come from the stylesheet's palette via `class` on the rect, so the
    chart follows light and dark mode like everything else.
    """
    if not rows:
        return ""

    bar_height, gap = 22, 8
    top = 6
    height = top * 2 + len(rows) * (bar_height + gap) - gap
    plot_width = max(60, width - label_width - 60)
    biggest = max((r.value for r in rows), default=0) or 1

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" '
        f'aria-label="Bar chart of {len(rows)} values">'
    ]
    for index, row in enumerate(rows):
        y = top + index * (bar_height + gap)
        length = max(1, round(plot_width * row.value / biggest))
        label = _escape(row.label)
        parts.append(
            f'<text class="chart-label" x="0" y="{y + 15}">{label}</text>'
            f'<rect class="chart-bar" x="{label_width}" y="{y}" '
            f'width="{length}" height="{bar_height}" rx="3"/>'
            f'<text class="chart-value" x="{label_width + length + 8}" '
            f'y="{y + 15}">{_escape(row.display)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _escape(text: str) -> str:
    """Escape for SVG text content. These labels are user-supplied item names."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
