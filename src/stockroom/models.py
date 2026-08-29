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

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>"


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
    out_qty: int = 0
    available: int = 0
    open_loan_count: int = 0

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

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
            return "All out"
        if self.out_qty > 0:
            return "Partially out"
        return "Available"


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
