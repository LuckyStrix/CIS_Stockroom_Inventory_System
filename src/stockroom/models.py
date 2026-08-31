"""Typed row wrappers.

Thin dataclasses over ``sqlite3.Row``. They exist so that callers get
attribute access and IDE completion instead of stringly-typed dict lookups,
and so the shape of each table is documented in one readable place. They hold
no behaviour and no database handle -- reads return them, and
:mod:`stockroom.service` takes plain arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class _Row:
    """Base: build a dataclass from a sqlite3.Row, ignoring extra columns."""

    @classmethod
    def from_row(cls, row: Any) -> Self:
        known = {f.name for f in fields(cls)}
        return cls(**{k: row[k] for k in row.keys() if k in known})

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class Person(_Row):
    id: int
    name: str
    email: str
    active: int
    notes: str
    created_at: str
    updated_at: str
    merged_into_id: int | None = None

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>"

    @property
    def is_merged(self) -> bool:
        """This record was folded into another; reads should follow the link."""
        return self.merged_into_id is not None


@dataclass(frozen=True, slots=True)
class Item(_Row):
    """An item plus its derived availability (from the ``item_status`` view).

    ``out_qty`` / ``available`` / ``open_loan_count`` default to a fully
    in-stock item so that a row read straight from the ``item`` table still
    produces a usable object.
    """

    id: int
    barcode: str | None
    name: str
    description: str
    product_url: str | None
    quantity: int
    unit: str
    shelf: str
    sub_location: str | None
    min_quantity: int | None
    created_at: str
    updated_at: str
    archived_at: str | None
    tracked: int = 0
    out_qty: int = 0
    held_qty: int = 0
    unaccounted_qty: int = 0
    available: int = 0
    open_loan_count: int = 0

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_tracked(self) -> bool:
        """Whether individual units of this item are tracked in ``unit``."""
        return bool(self.tracked)

    @property
    def location(self) -> str:
        """Human-readable location, skipping empty parts.

        e.g. "Unit B / Shelf 3 / Bin 12", or "Unassigned" if nothing is set.
        """
        parts = [p for p in (self.unit, self.shelf, self.sub_location) if p]
        return " / ".join(parts) if parts else "Unassigned"

    @property
    def is_low_stock(self) -> bool:
        """True when available has fallen to or below the configured floor."""
        return self.min_quantity is not None and self.available <= self.min_quantity

    @property
    def is_fully_out(self) -> bool:
        return self.quantity > 0 and self.available <= 0

    @property
    def status_label(self) -> str:
        if self.is_archived:
            return "Archived"
        if self.quantity == 0:
            return "None owned"
        if self.available <= 0:
            # Nothing lendable. Say *why*, because "all out" sends someone to
            # ask when it is due back and "all unavailable" does not.
            if self.out_qty > 0 and self.held_qty > 0:
                return "Out or unavailable"
            if self.held_qty > 0:
                return "Unavailable"
            return "All out"
        if self.held_qty > 0 and self.out_qty > 0:
            return "Partially out"
        if self.held_qty > 0:
            return "Some unavailable"
        if self.out_qty > 0:
            return "Partially out"
        return "Available"


# The condition an out-of-service unit is in. Ordered as the workflow runs:
# something breaks, someone sends it for repair, and it either comes back or
# it does not.
HOLD_STATES = ("broken", "repair", "missing", "gone")

HOLD_STATE_LABELS = {
    "broken": "Broken",
    "repair": "In repair",
    "missing": "Missing",
    "gone": "Written off",
}


@dataclass(frozen=True, slots=True)
class Unit(_Row):
    """One individual physical thing, from the ``unit_status`` view.

    ``state`` is derived from the unit's open hold, so it is 'ok' unless
    something is wrong; it is never stored on the unit itself, for the same
    reason availability is not stored on the item.
    """

    id: int
    item_id: int
    asset_tag: str | None
    serial: str | None
    note: str
    created_at: str
    updated_at: str
    retired_at: str | None
    item_name: str = ""
    item_barcode: str | None = None
    state: str = "ok"
    state_note: str | None = None
    hold_id: int | None = None

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None

    @property
    def is_available(self) -> bool:
        return self.state == "ok" and not self.is_retired

    @property
    def state_label(self) -> str:
        if self.is_retired:
            return "Retired"
        return HOLD_STATE_LABELS.get(self.state, "Available")

    @property
    def label(self) -> str:
        """How to name this unit to a human, best identifier first."""
        return self.asset_tag or self.serial or f"unit #{self.id}"


@dataclass(frozen=True, slots=True)
class Hold(_Row):
    """Units of an item that are not lendable, from the ``hold_detail`` view."""

    id: int
    item_id: int
    unit_id: int | None
    quantity: int
    state: str
    note: str
    loan_id: int | None
    opened_at: str
    opened_by: str
    closed_at: str | None
    closed_by: str | None
    resolution: str | None
    item_name: str = ""
    item_barcode: str | None = None
    asset_tag: str | None = None
    serial: str | None = None
    borrower_name: str | None = None
    borrower_email: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def state_label(self) -> str:
        return HOLD_STATE_LABELS.get(self.state, self.state)

    @property
    def is_unaccounted(self) -> bool:
        """Nobody knows where these units are, as opposed to knowing they are broken."""
        return self.state in ("missing", "gone")

    @property
    def what(self) -> str:
        """What this hold covers: a named unit, or a count."""
        if self.asset_tag or self.serial:
            return self.asset_tag or self.serial or ""
        return f"{self.quantity} x {self.item_name}"


@dataclass(frozen=True, slots=True)
class Photo(_Row):
    """One stored picture of an item. The file itself lives under PHOTO_DIR."""

    id: int
    item_id: int
    filename: str
    caption: str
    is_primary: int
    width: int | None
    height: int | None
    bytes: int | None
    created_at: str
    created_by: str
    deleted_at: str | None

    @property
    def url(self) -> str:
        return f"/photos/{self.filename}"

    @property
    def alt(self) -> str:
        """Alt text. Falls back to something honest rather than an empty string."""
        return self.caption or "Photo of this item"


@dataclass(frozen=True, slots=True)
class Loan(_Row):
    """A loan, optionally joined to item and person names (``loan_detail``)."""

    id: int
    item_id: int
    person_id: int
    quantity: int
    checked_out_at: str
    due_at: str | None
    returned_at: str | None
    checkout_note: str
    return_note: str
    checked_out_by: str
    returned_by: str | None
    split_from_loan_id: int | None
    item_name: str = ""
    item_barcode: str | None = None
    item_unit: str = ""
    item_shelf: str = ""
    person_name: str = ""
    person_email: str = ""

    @property
    def is_open(self) -> bool:
        return self.returned_at is None

    def is_overdue(self, now: str) -> bool:
        """Open, has a due date, and that date has passed.

        ``now`` is an ISO-8601 UTC string; the format sorts lexicographically,
        so a string comparison is a correct date comparison here.
        """
        return self.is_open and self.due_at is not None and self.due_at < now


@dataclass(frozen=True, slots=True)
class Event(_Row):
    """One entry in the append-only audit log."""

    id: int
    at: str
    actor: str
    action: str
    entity_type: str
    entity_id: int | None
    item_id: int | None
    person_id: int | None
    summary: str
    changes_json: str | None

    @property
    def changes(self) -> dict[str, dict[str, Any]]:
        """Parsed field-level diff: ``{"shelf": {"from": "3", "to": "1"}}``."""
        if not self.changes_json:
            return {}
        try:
            return json.loads(self.changes_json)
        except json.JSONDecodeError:
            return {}
