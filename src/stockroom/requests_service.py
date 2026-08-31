"""The three request workflows, and confirmed stockroom open hours.

    ======================================================================
    THE RULE: every mutation here writes its `event` row in the same
    transaction as the change. Sibling of service.py and accounts.py.
    ======================================================================

Three forms, one lifecycle:

    borrow      "I would like to take out the Canon body next Tuesday"
    new_item    "The stockroom should own a second tripod"
    open_hours  "Could someone be there Thursday afternoon so I can return this?"

    pending ──approve──> approved ──fulfil──> fulfilled
        │                    │
        ├──decline──> declined
        └──cancel───> cancelled          (requester withdrawing their own)

They share a table because they share that lifecycle, one staff inbox and one
audit path. What differs is a handful of fields, which the schema's CHECK
constraint pins down per kind.

**Approval does not move equipment.** Approving a borrow request means "yes,
you may have this" -- the loan is created separately when someone physically
hands the item over, because that is the moment the stockroom's shelf actually
changes. Fulfilment routes through the existing `service.checkout()`, so every
availability invariant and audit guarantee applies unchanged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, service
from .service import (
    Actor,
    ConflictError,
    NotFound,
    ValidationError,
    _clean,
    _optional,
    _require,
    log_event,
)

KINDS = ("borrow", "new_item", "open_hours")
OPEN_STATUSES = ("pending", "approved")

KIND_LABELS = {
    "borrow": "Borrow equipment",
    "new_item": "Add to inventory",
    "open_hours": "Open the stockroom",
}


@dataclass(frozen=True, slots=True)
class Request:
    id: int
    kind: str
    requester_id: int
    status: str
    created_at: str
    updated_at: str
    requester_note: str
    decided_at: str | None
    decided_by_id: int | None
    decision_note: str
    item_id: int | None
    quantity: int | None
    needed_from: str | None
    needed_until: str | None
    proposed_name: str | None
    proposed_description: str | None
    proposed_url: str | None
    proposed_quantity: int | None
    proposed_vendor: str | None
    window_start: str | None
    window_end: str | None
    purpose: str | None
    loan_id: int | None
    created_item_id: int | None
    requester_name: str = ""
    requester_email: str = ""
    decided_by_name: str | None = None
    item_name: str | None = None
    item_barcode: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Request:
        known = cls.__slots__
        return cls(**{k: row[k] for k in row.keys() if k in known})

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def can_fulfil(self) -> bool:
        """Approved, and there is something concrete left to do."""
        return self.status == "approved" and self.kind in ("borrow", "new_item")

    @property
    def age_days(self) -> float:
        """How long this has been waiting, in days.

        Nothing in this system emails anybody. A request is worked only if a
        human sees it sitting there, so how long it has been sitting is the
        single most useful thing to show about it.
        """
        stamp = _parse_stamp(self.created_at)
        if stamp is None:
            return 0.0
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400

    @property
    def is_stale(self) -> bool:
        return self.is_open and self.age_days > config.REQUEST_STALE_DAYS

    @property
    def age_label(self) -> str:
        days = self.age_days
        if days < 1:
            return "today"
        if days < 2:
            return "yesterday"
        return f"{days:.0f} days ago"

    @property
    def title(self) -> str:
        """One line describing what was asked for."""
        if self.kind == "borrow":
            return f"{self.quantity} x {self.item_name or 'item'}"
        if self.kind == "new_item":
            return self.proposed_name or "new item"
        return f"{self.window_start or ''} to {self.window_end or ''}".strip()


@dataclass(frozen=True, slots=True)
class OpenHours:
    id: int
    window_start: str
    window_end: str
    note: str
    published: int
    request_id: int | None
    created_at: str
    created_by: str
    cancelled_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> OpenHours:
        known = cls.__slots__
        return cls(**{k: row[k] for k in row.keys() if k in known})


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _parse_stamp(stamp: str | None) -> datetime | None:
    """Parse one of the ISO-8601 UTC strings db.utcnow() produces."""
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def get_request(conn: sqlite3.Connection, request_id: int) -> Request:
    row = conn.execute(
        "SELECT * FROM request_detail WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"No request with id {request_id}.")
    return Request.from_row(row)


def list_requests(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    requester_id: int | None = None,
    open_only: bool = False,
    limit: int = 200,
) -> list[Request]:
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if requester_id is not None:
        where.append("requester_id = ?")
        params.append(requester_id)
    if open_only:
        where.append("status IN ('pending', 'approved')")

    sql = "SELECT * FROM request_detail"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Pending first: the inbox exists to surface what still needs a decision.
    sql += (
        " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 "
        "ELSE 2 END, created_at DESC LIMIT ?"
    )
    params.append(int(limit))
    return [Request.from_row(r) for r in conn.execute(sql, params)]


def count_pending(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM request WHERE status = 'pending'"
        ).fetchone()["n"]
    )


def count_stale(conn: sqlite3.Connection) -> int:
    """Pending requests older than the staleness threshold."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=config.REQUEST_STALE_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM request WHERE status = 'pending' "
            "AND created_at < ?",
            (cutoff,),
        ).fetchone()["n"]
    )


def overlapping_requests(
    conn: sqlite3.Connection, request: Request
) -> list[Request]:
    """Other live borrow requests competing for the same item.

    Advisory only. Approving a request still reserves nothing -- see
    submit_borrow, which is right that a request is a conversation, not a
    hold, and that the availability figure is the one number this system must
    never get wrong.

    What this fixes is narrower and real: staff had no way to notice they were
    promising the same last camera to two people for the same weekend. It
    tells them; it does not decide for them.

    A NULL window means open-ended, so it overlaps everything. Because the
    columns are ISO-8601 UTC strings they compare correctly as text, which is
    the same property the overdue query relies on.
    """
    if request.kind != "borrow" or request.item_id is None:
        return []

    rows = conn.execute(
        """
        SELECT * FROM request_detail
        WHERE kind = 'borrow'
          AND item_id = ?
          AND id <> ?
          AND status IN ('pending', 'approved')
          AND (? IS NULL OR needed_until IS NULL OR needed_until >= ?)
          AND (? IS NULL OR needed_from  IS NULL OR needed_from  <= ?)
        ORDER BY status, created_at
        """,
        (request.item_id, request.id,
         request.needed_from, request.needed_from,
         request.needed_until, request.needed_until),
    )
    return [Request.from_row(r) for r in rows]


def competing_demand(conn: sqlite3.Connection, request: Request) -> int:
    """Units already spoken for by overlapping requests, plus this one."""
    others = overlapping_requests(conn, request)
    return (request.quantity or 0) + sum(r.quantity or 0 for r in others)


# ---------------------------------------------------------------------------
# submitting
# ---------------------------------------------------------------------------


def submit_borrow(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    requester_id: int,
    item_id: int,
    quantity: int = 1,
    needed_from: str | None = None,
    needed_until: str | None = None,
    note: str = "",
) -> Request:
    """Ask to borrow equipment.

    Availability is *not* reserved here. A request is a conversation, not a
    hold -- reserving stock on an unapproved request would make the shelf
    disagree with reality, and the availability figure is the one number this
    system must never get wrong.
    """
    quantity = service._as_int(quantity, "Quantity", minimum=1)

    with db.transaction(conn):
        item = service.get_item(conn, item_id)
        if item.is_archived:
            raise ConflictError(f"{item.name} is archived and cannot be requested.")
        if quantity > item.quantity:
            raise ConflictError(
                f"The stockroom only owns {item.quantity} of {item.name}."
            )
        request_id = _insert(
            conn,
            kind="borrow",
            requester_id=requester_id,
            note=note,
            item_id=item_id,
            quantity=quantity,
            needed_from=_optional(needed_from),
            needed_until=_optional(needed_until),
        )
        log_event(
            conn,
            actor=actor,
            action="request.create",
            entity_type="request",
            entity_id=request_id,
            item_id=item_id,
            summary=f"Requested to borrow {quantity} x {item.name}",
        )
        return get_request(conn, request_id)


def submit_new_item(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    requester_id: int,
    name: str,
    description: str = "",
    url: str | None = None,
    quantity: int = 1,
    vendor: str | None = None,
    note: str = "",
) -> Request:
    """Ask for something to be added to the stockroom."""
    name = _require(name, "Item name")
    quantity = service._as_int(quantity, "Quantity", minimum=1)

    with db.transaction(conn):
        request_id = _insert(
            conn,
            kind="new_item",
            requester_id=requester_id,
            note=note,
            proposed_name=name,
            proposed_description=_clean(description),
            proposed_url=_optional(url),
            proposed_quantity=quantity,
            proposed_vendor=_optional(vendor),
        )
        log_event(
            conn,
            actor=actor,
            action="request.create",
            entity_type="request",
            entity_id=request_id,
            summary=f"Requested that the stockroom stock {quantity} x {name}",
        )
        return get_request(conn, request_id)


def submit_open_hours(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    requester_id: int,
    window_start: str,
    window_end: str,
    purpose: str = "both",
    note: str = "",
) -> Request:
    """Ask for the stockroom to be staffed at a particular time."""
    window_start = _require(window_start, "Start time")
    window_end = _require(window_end, "End time")
    if purpose not in ("borrow", "return", "both"):
        raise ValidationError("Purpose must be borrow, return or both.")
    if window_end <= window_start:
        raise ValidationError("The end of the window must be after the start.")

    with db.transaction(conn):
        request_id = _insert(
            conn,
            kind="open_hours",
            requester_id=requester_id,
            note=note,
            window_start=window_start,
            window_end=window_end,
            purpose=purpose,
        )
        log_event(
            conn,
            actor=actor,
            action="request.create",
            entity_type="request",
            entity_id=request_id,
            summary=f"Requested the stockroom be open {window_start} to {window_end}",
        )
        return get_request(conn, request_id)


def _insert(conn: sqlite3.Connection, *, kind: str, requester_id: int,
            note: str, **fields: Any) -> int:
    now = db.utcnow()
    columns = ["kind", "requester_id", "created_at", "updated_at", "requester_note"]
    values: list[Any] = [kind, requester_id, now, now, _clean(note)]
    for column, value in fields.items():
        columns.append(column)
        values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO request ({', '.join(columns)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# deciding
# ---------------------------------------------------------------------------


def approve(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    request_id: int,
    decided_by_id: int,
    note: str = "",
) -> Request:
    """Approve a request.

    For open-hours requests this is the whole job -- approval publishes a
    confirmed slot. For the other two it grants permission; the equipment or
    the new item follows at :func:`fulfil`.
    """
    with db.transaction(conn):
        request = get_request(conn, request_id)
        _require_pending(request)

        _decide(conn, request_id, "approved", decided_by_id, note)

        summary = f"Approved: {request.title}"
        if request.kind == "open_hours":
            _create_open_hours(
                conn,
                actor=actor,
                window_start=request.window_start or "",
                window_end=request.window_end or "",
                note=request.requester_note,
                request_id=request_id,
            )
            # Nothing further to do, so this request is already complete.
            conn.execute(
                "UPDATE request SET status = 'fulfilled', updated_at = ? WHERE id = ?",
                (db.utcnow(), request_id),
            )
            summary = f"Confirmed stockroom open hours: {request.title}"

        log_event(
            conn,
            actor=actor,
            action="request.approve",
            entity_type="request",
            entity_id=request_id,
            item_id=request.item_id,
            summary=summary,
            changes={"status": {"from": request.status, "to": "approved"}},
        )
        return get_request(conn, request_id)


def decline(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    request_id: int,
    decided_by_id: int,
    note: str = "",
) -> Request:
    with db.transaction(conn):
        request = get_request(conn, request_id)
        if request.status not in ("pending", "approved"):
            raise ConflictError(f"That request is already {request.status}.")
        _decide(conn, request_id, "declined", decided_by_id, note)
        log_event(
            conn,
            actor=actor,
            action="request.decline",
            entity_type="request",
            entity_id=request_id,
            item_id=request.item_id,
            summary=f"Declined: {request.title}"
                    + (f" ({_clean(note)})" if _clean(note) else ""),
            changes={"status": {"from": request.status, "to": "declined"}},
        )
        return get_request(conn, request_id)


def cancel(
    conn: sqlite3.Connection, *, actor: Actor, request_id: int, by_account_id: int
) -> Request:
    """Withdraw a request. Only the person who filed it, or staff."""
    with db.transaction(conn):
        request = get_request(conn, request_id)
        if not request.is_open:
            raise ConflictError(f"That request is already {request.status}.")
        conn.execute(
            "UPDATE request SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (db.utcnow(), request_id),
        )
        log_event(
            conn,
            actor=actor,
            action="request.cancel",
            entity_type="request",
            entity_id=request_id,
            item_id=request.item_id,
            summary=f"Cancelled: {request.title}",
            changes={"status": {"from": request.status, "to": "cancelled"}},
        )
        return get_request(conn, request_id)


def _require_pending(request: Request) -> None:
    if request.status != "pending":
        raise ConflictError(f"That request is already {request.status}.")


def _decide(
    conn: sqlite3.Connection, request_id: int, status: str,
    decided_by_id: int, note: str,
) -> None:
    now = db.utcnow()
    conn.execute(
        "UPDATE request SET status = ?, decided_at = ?, decided_by_id = ?, "
        "decision_note = ?, updated_at = ? WHERE id = ?",
        (status, now, decided_by_id, _clean(note), now, request_id),
    )


# ---------------------------------------------------------------------------
# fulfilling
# ---------------------------------------------------------------------------


def fulfil_borrow(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    request_id: int,
    quantity: int | None = None,
    due_at: str | None = None,
) -> Request:
    """Hand the equipment over: turn an approved borrow request into a loan.

    Delegates to :func:`service.checkout`, so availability limits, the
    concurrency guard and the loan audit trail all apply exactly as they do at
    the counter. Nothing about requests bypasses the inventory rules.
    """
    with db.transaction(conn):
        request = get_request(conn, request_id)
        if request.kind != "borrow":
            raise ConflictError("Only borrow requests are fulfilled with a checkout.")
        if request.status != "approved":
            raise ConflictError(
                f"That request is {request.status}; approve it before checking out."
            )

        from .accounts import get_account

        requester = get_account(conn, request.requester_id)
        if requester.person_id is None:
            raise ConflictError(
                f"{requester.name} has no borrower record yet. Approve their "
                "account first."
            )

        loan = service.checkout(
            conn,
            actor=actor,
            item_id=request.item_id,
            person_id=requester.person_id,
            quantity=quantity or request.quantity or 1,
            due_at=due_at or request.needed_until,
            note=f"From request #{request_id}",
        )
        conn.execute(
            "UPDATE request SET status = 'fulfilled', loan_id = ?, updated_at = ? "
            "WHERE id = ?",
            (loan.id, db.utcnow(), request_id),
        )
        log_event(
            conn,
            actor=actor,
            action="request.fulfil",
            entity_type="request",
            entity_id=request_id,
            item_id=request.item_id,
            summary=f"Fulfilled request #{request_id}: checked out {loan.quantity} "
                    f"x {loan.item_name} to {requester.name}",
            changes={"loan_id": {"from": None, "to": loan.id}},
        )
        return get_request(conn, request_id)


def fulfil_new_item(
    conn: sqlite3.Connection, *, actor: Actor, request_id: int, item_id: int
) -> Request:
    """Mark a new-item request satisfied by an item that now exists."""
    with db.transaction(conn):
        request = get_request(conn, request_id)
        if request.kind != "new_item":
            raise ConflictError("That request is not a new-item request.")
        if request.status != "approved":
            raise ConflictError(f"That request is {request.status}.")

        item = service.get_item(conn, item_id)
        conn.execute(
            "UPDATE request SET status = 'fulfilled', created_item_id = ?, "
            "updated_at = ? WHERE id = ?",
            (item_id, db.utcnow(), request_id),
        )
        log_event(
            conn,
            actor=actor,
            action="request.fulfil",
            entity_type="request",
            entity_id=request_id,
            item_id=item_id,
            summary=f"Fulfilled request #{request_id}: added {item.name} to the stockroom",
            changes={"created_item_id": {"from": None, "to": item_id}},
        )
        return get_request(conn, request_id)


# ---------------------------------------------------------------------------
# open hours
# ---------------------------------------------------------------------------


def _create_open_hours(
    conn: sqlite3.Connection, *, actor: Actor, window_start: str, window_end: str,
    note: str = "", request_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO open_hours (window_start, window_end, note, request_id,
                                created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (window_start, window_end, _clean(note), request_id, db.utcnow(), str(actor)),
    )
    return int(cur.lastrowid)


def add_open_hours(
    conn: sqlite3.Connection, *, actor: Actor, window_start: str,
    window_end: str, note: str = "",
) -> OpenHours:
    """Staff publishing a staffed window directly, with no request behind it."""
    window_start = _require(window_start, "Start time")
    window_end = _require(window_end, "End time")
    if window_end <= window_start:
        raise ValidationError("The end of the window must be after the start.")

    with db.transaction(conn):
        slot_id = _create_open_hours(
            conn, actor=actor, window_start=window_start,
            window_end=window_end, note=note,
        )
        log_event(
            conn,
            actor=actor,
            action="open_hours.create",
            entity_type="open_hours",
            entity_id=slot_id,
            summary=f"Published open hours {window_start} to {window_end}",
        )
        return get_open_hours(conn, slot_id)


def cancel_open_hours(
    conn: sqlite3.Connection, *, actor: Actor, slot_id: int
) -> OpenHours:
    with db.transaction(conn):
        slot = get_open_hours(conn, slot_id)
        if slot.cancelled_at is not None:
            raise ConflictError("That slot is already cancelled.")
        conn.execute(
            "UPDATE open_hours SET cancelled_at = ? WHERE id = ?",
            (db.utcnow(), slot_id),
        )
        log_event(
            conn,
            actor=actor,
            action="open_hours.cancel",
            entity_type="open_hours",
            entity_id=slot_id,
            summary=f"Cancelled open hours {slot.window_start} to {slot.window_end}",
        )
        return get_open_hours(conn, slot_id)


def get_open_hours(conn: sqlite3.Connection, slot_id: int) -> OpenHours:
    row = conn.execute("SELECT * FROM open_hours WHERE id = ?", (slot_id,)).fetchone()
    if row is None:
        raise NotFound(f"No open-hours slot with id {slot_id}.")
    return OpenHours.from_row(row)


def list_open_hours(
    conn: sqlite3.Connection, *, upcoming_only: bool = True, limit: int = 50
) -> list[OpenHours]:
    """Confirmed staffed windows, soonest first.

    ``upcoming_only`` is what the public page uses -- a slot that has already
    passed answers nobody's question.
    """
    sql = "SELECT * FROM open_hours WHERE cancelled_at IS NULL"
    params: list[Any] = []
    if upcoming_only:
        sql += " AND window_end >= ?"
        params.append(db.utcnow())
    sql += " ORDER BY window_start LIMIT ?"
    params.append(int(limit))
    return [OpenHours.from_row(r) for r in conn.execute(sql, params)]
