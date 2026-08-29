"""The service layer -- every mutation in the system lives here.

    ======================================================================
    THE RULE: every function that changes data writes its `event` row in
    the SAME transaction as the change. Nothing outside this module writes
    to `item`, `loan` or `person`.
    ======================================================================

That rule is the whole reason this layer exists. The web routes, the CLI and
the CSV importer all call these functions; none of them touch the tables
directly. Because the change and its audit row commit together, it is not
possible for a change to land without a corresponding history entry, or for
a history entry to describe a change that rolled back.

`tests/test_audit.py` enforces it by calling every public mutating function
and asserting an event appears.

Reads are also here for convenience, but they are unremarkable -- the
interesting logic is the invariants:

* You cannot check out more than is available.
* You cannot reduce an item's total quantity below the number currently out.
* Returning part of a loan splits it rather than editing it, keeping the
  trail append-only.
* An item with open loans cannot be archived.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import config, db
from .models import Event, Item, Loan, Person

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class StockroomError(Exception):
    """Base for errors this layer raises deliberately."""


class NotFound(StockroomError):
    """A requested record does not exist."""


class ValidationError(StockroomError):
    """The caller supplied something invalid (empty name, bad email, ...)."""


class ConflictError(StockroomError):
    """The request is well-formed but violates an invariant.

    Raised for things like checking out more units than are available. These
    are expected, user-facing conditions -- the web layer renders them as a
    message, not a 500.
    """


# ---------------------------------------------------------------------------
# actor -- who is performing the change
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """The operator making a change, recorded on every event.

    Today this comes from a cookie set by the "Who are you?" prompt. After
    the RIT SSO integration it will come from the Shibboleth session instead
    -- see docs/sso-integration.md. Nothing else about this module changes.
    """

    name: str
    email: str = ""

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>" if self.email else self.name

    @classmethod
    def system(cls, what: str = "system") -> Actor:
        """For changes made by the machine (imports, migrations, scripts)."""
        return cls(name=what)


SYSTEM = Actor.system()


# ---------------------------------------------------------------------------
# the audit log
# ---------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    *,
    actor: Actor | str,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    summary: str,
    item_id: int | None = None,
    person_id: int | None = None,
    changes: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Append one row to the audit log. Call only inside a transaction."""
    cur = conn.execute(
        """
        INSERT INTO event (at, actor, action, entity_type, entity_id,
                           item_id, person_id, summary, changes_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            db.utcnow(),
            str(actor),
            action,
            entity_type,
            entity_id,
            item_id,
            person_id,
            summary,
            json.dumps(changes, sort_keys=True) if changes else None,
        ),
    )
    return int(cur.lastrowid)


# The publisher hook. publish.worker installs a callback here at startup; when
# nothing is installed (tests, CLI one-shots) this is a no-op, so the service
# layer has no hard dependency on the publishing machinery.
_change_listener = None


def set_change_listener(callback) -> None:
    """Register a zero-argument callable invoked after each committed change."""
    global _change_listener
    _change_listener = callback


def _notify_change() -> None:
    """Signal that the inventory changed.

    Called *after* the transaction commits, never inside it: a slow or broken
    publisher must not hold a write lock, and a publish failure must not roll
    back a checkout that already succeeded.
    """
    if _change_listener is None:
        return
    try:
        _change_listener()
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger(__name__).exception("change listener failed")


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _require(value: str | None, field: str) -> str:
    cleaned = _clean(value)
    if not cleaned:
        raise ValidationError(f"{field} is required.")
    return cleaned


def _clean_email(value: str | None) -> str:
    email = _require(value, "Email").lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError(f"{email!r} does not look like an email address.")
    return email


def _optional(value: str | None) -> str | None:
    """Normalize blank strings to NULL so 'unset' has one representation."""
    cleaned = _clean(value)
    return cleaned or None


def _as_int(value: Any, field: str, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a whole number.") from None
    if number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    return number


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Field-level before/after for the fields present in ``after``."""
    return {
        key: {"from": before.get(key), "to": value}
        for key, value in after.items()
        if before.get(key) != value
    }


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------


def get_person(conn: sqlite3.Connection, person_id: int) -> Person:
    row = conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise NotFound(f"No person with id {person_id}.")
    return Person.from_row(row)


def find_person_by_email(conn: sqlite3.Connection, email: str) -> Person | None:
    row = conn.execute(
        "SELECT * FROM person WHERE email = ? COLLATE NOCASE", (_clean(email).lower(),)
    ).fetchone()
    return Person.from_row(row) if row else None


def list_people(conn: sqlite3.Connection, *, include_inactive: bool = False) -> list[Person]:
    sql = "SELECT * FROM person"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return [Person.from_row(r) for r in conn.execute(sql)]


def create_person(
    conn: sqlite3.Connection, *, actor: Actor, name: str, email: str, notes: str = ""
) -> Person:
    name = _require(name, "Name")
    email = _clean_email(email)

    with db.transaction(conn):
        if find_person_by_email(conn, email) is not None:
            raise ConflictError(f"A person with email {email} already exists.")
        now = db.utcnow()
        cur = conn.execute(
            """INSERT INTO person (name, email, active, notes, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?, ?)""",
            (name, email, _clean(notes), now, now),
        )
        person_id = int(cur.lastrowid)
        log_event(
            conn,
            actor=actor,
            action="person.create",
            entity_type="person",
            entity_id=person_id,
            person_id=person_id,
            summary=f"Added person {name} <{email}>",
            changes={"name": {"from": None, "to": name},
                     "email": {"from": None, "to": email}},
        )
        person = get_person(conn, person_id)
    _notify_change()
    return person


def get_or_create_person(
    conn: sqlite3.Connection, *, actor: Actor, name: str, email: str
) -> Person:
    """Look a person up by email, creating them if they are new.

    The lookup wins on conflict: if the email is known but the supplied name
    differs, the stored name is kept. A typo at the checkout counter should
    not silently rename someone's record -- use :func:`update_person` to
    correct a name deliberately.
    """
    existing = find_person_by_email(conn, email)
    if existing is not None:
        return existing
    return create_person(conn, actor=actor, name=name, email=email)


def update_person(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    person_id: int,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    active: bool | None = None,
) -> Person:
    """Update a person. Only the arguments you pass are changed."""
    with db.transaction(conn):
        current = get_person(conn, person_id)
        changed: dict[str, Any] = {}
        if name is not None:
            changed["name"] = _require(name, "Name")
        if email is not None:
            new_email = _clean_email(email)
            other = find_person_by_email(conn, new_email)
            if other is not None and other.id != person_id:
                raise ConflictError(f"{new_email} is already used by {other.name}.")
            changed["email"] = new_email
        if notes is not None:
            changed["notes"] = _clean(notes)
        if active is not None:
            changed["active"] = int(active)

        changes = _diff(current.as_dict(), changed)
        if not changes:
            return current

        changed["updated_at"] = db.utcnow()
        assignments = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE person SET {assignments} WHERE id = ?",
            (*changed.values(), person_id),
        )
        log_event(
            conn,
            actor=actor,
            action="person.update",
            entity_type="person",
            entity_id=person_id,
            person_id=person_id,
            summary=f"Updated {current.name} ({_summarize_fields(changes)})",
            changes=changes,
        )
        person = get_person(conn, person_id)
    _notify_change()
    return person


def _summarize_fields(changes: dict[str, dict[str, Any]]) -> str:
    return ", ".join(sorted(changes))


# ---------------------------------------------------------------------------
# barcodes
# ---------------------------------------------------------------------------


def next_barcode(conn: sqlite3.Connection) -> str:
    """Reserve and return the next sequential barcode, e.g. ``CIS-000142``.

    The counter lives in the ``meta`` table so it is captured by database
    backups. The loop guards against collisions with a manufacturer barcode
    that happens to match our format and was entered by hand.
    """
    while True:
        counter = int(db.get_meta(conn, "barcode_counter", "0") or "0") + 1
        db.set_meta(conn, "barcode_counter", str(counter))
        code = f"{config.BARCODE_PREFIX}-{counter:0{config.BARCODE_DIGITS}d}"
        taken = conn.execute("SELECT 1 FROM item WHERE barcode = ?", (code,)).fetchone()
        if taken is None:
            return code


def assign_barcode(conn: sqlite3.Connection, *, actor: Actor, item_id: int) -> Item:
    """Give an item a generated barcode. Errors if it already has one."""
    with db.transaction(conn):
        item = get_item(conn, item_id)
        if item.barcode:
            raise ConflictError(f"{item.name} already has barcode {item.barcode}.")
        code = next_barcode(conn)
        conn.execute(
            "UPDATE item SET barcode = ?, updated_at = ? WHERE id = ?",
            (code, db.utcnow(), item_id),
        )
        log_event(
            conn,
            actor=actor,
            action="item.update",
            entity_type="item",
            entity_id=item_id,
            item_id=item_id,
            summary=f"Assigned barcode {code} to {item.name}",
            changes={"barcode": {"from": None, "to": code}},
        )
        updated = get_item(conn, item_id)
    _notify_change()
    return updated


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

_ITEM_FIELDS = (
    "name", "description", "product_url", "quantity",
    "unit", "shelf", "sub_location", "min_quantity", "barcode",
)
_LOCATION_FIELDS = {"unit", "shelf", "sub_location"}


def get_item(conn: sqlite3.Connection, item_id: int) -> Item:
    row = conn.execute("SELECT * FROM item_status WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFound(f"No item with id {item_id}.")
    return Item.from_row(row)


def get_item_by_barcode(conn: sqlite3.Connection, barcode: str) -> Item | None:
    code = _clean(barcode)
    if not code:
        return None
    row = conn.execute(
        "SELECT * FROM item_status WHERE barcode = ? COLLATE NOCASE", (code,)
    ).fetchone()
    return Item.from_row(row) if row else None


def list_items(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
    unit: str | None = None,
    only_available: bool = False,
    only_out: bool = False,
    only_low_stock: bool = False,
) -> list[Item]:
    """List items with availability, newest filters applied in SQL."""
    where: list[str] = []
    params: list[Any] = []
    if not include_archived:
        where.append("archived_at IS NULL")
    if unit:
        where.append("unit = ?")
        params.append(unit)
    if only_available:
        where.append("available > 0")
    if only_out:
        where.append("out_qty > 0")
    if only_low_stock:
        where.append("min_quantity IS NOT NULL AND available <= min_quantity")

    sql = "SELECT * FROM item_status"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name COLLATE NOCASE"
    return [Item.from_row(r) for r in conn.execute(sql, params)]


def list_units(conn: sqlite3.Connection) -> list[str]:
    """Distinct storage units, for filter dropdowns and datalists."""
    rows = conn.execute(
        "SELECT DISTINCT unit FROM item WHERE unit <> '' AND archived_at IS NULL "
        "ORDER BY unit COLLATE NOCASE"
    )
    return [r["unit"] for r in rows]


def create_item(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    name: str,
    description: str = "",
    quantity: int = 1,
    unit: str = "",
    shelf: str = "",
    sub_location: str | None = None,
    product_url: str | None = None,
    barcode: str | None = None,
    min_quantity: int | None = None,
    generate_barcode: bool = True,
) -> Item:
    """Add a new item.

    If ``barcode`` is omitted and ``generate_barcode`` is true (the default),
    a sequential ``CIS-000123`` code is assigned so the item can be labelled
    and scanned immediately.
    """
    name = _require(name, "Name")
    quantity = _as_int(quantity, "Quantity", minimum=0)
    if min_quantity is not None and min_quantity != "":
        min_quantity = _as_int(min_quantity, "Minimum quantity", minimum=0)
    else:
        min_quantity = None

    with db.transaction(conn):
        code = _optional(barcode)
        if code is not None:
            clash = get_item_by_barcode(conn, code)
            if clash is not None:
                raise ConflictError(f"Barcode {code} is already used by {clash.name}.")
        elif generate_barcode:
            code = next_barcode(conn)

        now = db.utcnow()
        cur = conn.execute(
            """
            INSERT INTO item (barcode, name, description, product_url, quantity,
                              unit, shelf, sub_location, min_quantity,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (code, name, _clean(description), _optional(product_url), quantity,
             _clean(unit), _clean(shelf), _optional(sub_location), min_quantity,
             now, now),
        )
        item_id = int(cur.lastrowid)
        item = get_item(conn, item_id)
        log_event(
            conn,
            actor=actor,
            action="item.create",
            entity_type="item",
            entity_id=item_id,
            item_id=item_id,
            summary=f"Created {name} (qty {quantity}) at {item.location}",
            changes={f: {"from": None, "to": getattr(item, f)} for f in _ITEM_FIELDS},
        )
    _notify_change()
    return item


def update_item(
    conn: sqlite3.Connection, *, actor: Actor, item_id: int, **updates: Any
) -> Item:
    """Update any subset of an item's fields.

    Accepts the field names in ``_ITEM_FIELDS``. The action recorded is the
    most specific one that fits -- a pure location change logs as
    ``item.relocate`` and a pure count change as ``item.quantity_adjust`` --
    so the history can be filtered meaningfully. Passing an unchanged value
    is a no-op and writes no event.
    """
    unknown = set(updates) - set(_ITEM_FIELDS)
    if unknown:
        raise ValidationError(f"Unknown item field(s): {', '.join(sorted(unknown))}")

    with db.transaction(conn):
        current = get_item(conn, item_id)
        changed: dict[str, Any] = {}

        if "name" in updates:
            changed["name"] = _require(updates["name"], "Name")
        if "description" in updates:
            changed["description"] = _clean(updates["description"])
        if "product_url" in updates:
            changed["product_url"] = _optional(updates["product_url"])
        if "unit" in updates:
            changed["unit"] = _clean(updates["unit"])
        if "shelf" in updates:
            changed["shelf"] = _clean(updates["shelf"])
        if "sub_location" in updates:
            changed["sub_location"] = _optional(updates["sub_location"])
        if "min_quantity" in updates:
            raw = updates["min_quantity"]
            changed["min_quantity"] = (
                None if raw is None or raw == ""
                else _as_int(raw, "Minimum quantity", minimum=0)
            )
        if "barcode" in updates:
            code = _optional(updates["barcode"])
            if code is not None:
                clash = get_item_by_barcode(conn, code)
                if clash is not None and clash.id != item_id:
                    raise ConflictError(
                        f"Barcode {code} is already used by {clash.name}."
                    )
            changed["barcode"] = code
        if "quantity" in updates:
            new_qty = _as_int(updates["quantity"], "Quantity", minimum=0)
            # The one invariant that cannot be expressed as a CHECK constraint:
            # total owned may never fall below what is currently lent out.
            if new_qty < current.out_qty:
                raise ConflictError(
                    f"Cannot set quantity to {new_qty}: {current.out_qty} "
                    f"{'unit is' if current.out_qty == 1 else 'units are'} "
                    "currently checked out."
                )
            changed["quantity"] = new_qty

        changes = _diff(current.as_dict(), changed)
        if not changes:
            return current

        touched = set(changes)
        if touched <= _LOCATION_FIELDS:
            action = "item.relocate"
            after = get_item_preview_location(current, changes)
            summary = f"Moved {current.name} from {current.location} to {after}"
        elif touched == {"quantity"}:
            action = "item.quantity_adjust"
            summary = (
                f"Quantity of {current.name}: "
                f"{changes['quantity']['from']} to {changes['quantity']['to']}"
            )
        else:
            action = "item.update"
            summary = f"Updated {current.name} ({_summarize_fields(changes)})"

        changed["updated_at"] = db.utcnow()
        assignments = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE item SET {assignments} WHERE id = ?",
            (*changed.values(), item_id),
        )
        log_event(
            conn,
            actor=actor,
            action=action,
            entity_type="item",
            entity_id=item_id,
            item_id=item_id,
            summary=summary,
            changes=changes,
        )
        item = get_item(conn, item_id)
    _notify_change()
    return item


def get_item_preview_location(item: Item, changes: dict[str, dict[str, Any]]) -> str:
    """The location string an item will have once ``changes`` are applied.

    Used only to write a readable relocation summary before the UPDATE runs.
    """
    parts = [
        changes.get(field, {}).get("to", getattr(item, field))
        for field in ("unit", "shelf", "sub_location")
    ]
    present = [p for p in parts if p]
    return " / ".join(present) if present else "Unassigned"


def archive_item(
    conn: sqlite3.Connection, *, actor: Actor, item_id: int, reason: str = ""
) -> Item:
    """Retire an item. Rows are never deleted, so history stays intact."""
    with db.transaction(conn):
        item = get_item(conn, item_id)
        if item.is_archived:
            raise ConflictError(f"{item.name} is already archived.")
        if item.out_qty > 0:
            raise ConflictError(
                f"Cannot archive {item.name}: {item.out_qty} "
                f"{'unit is' if item.out_qty == 1 else 'units are'} still checked out."
            )
        now = db.utcnow()
        conn.execute(
            "UPDATE item SET archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, item_id),
        )
        summary = f"Archived {item.name}"
        if _clean(reason):
            summary += f" ({_clean(reason)})"
        log_event(
            conn,
            actor=actor,
            action="item.archive",
            entity_type="item",
            entity_id=item_id,
            item_id=item_id,
            summary=summary,
            changes={"archived_at": {"from": None, "to": now}},
        )
        archived = get_item(conn, item_id)
    _notify_change()
    return archived


def restore_item(conn: sqlite3.Connection, *, actor: Actor, item_id: int) -> Item:
    """Un-archive an item."""
    with db.transaction(conn):
        item = get_item(conn, item_id)
        if not item.is_archived:
            raise ConflictError(f"{item.name} is not archived.")
        conn.execute(
            "UPDATE item SET archived_at = NULL, updated_at = ? WHERE id = ?",
            (db.utcnow(), item_id),
        )
        log_event(
            conn,
            actor=actor,
            action="item.restore",
            entity_type="item",
            entity_id=item_id,
            item_id=item_id,
            summary=f"Restored {item.name} from the archive",
            changes={"archived_at": {"from": item.archived_at, "to": None}},
        )
        restored = get_item(conn, item_id)
    _notify_change()
    return restored


# ---------------------------------------------------------------------------
# loans
# ---------------------------------------------------------------------------


def get_loan(conn: sqlite3.Connection, loan_id: int) -> Loan:
    row = conn.execute("SELECT * FROM loan_detail WHERE id = ?", (loan_id,)).fetchone()
    if row is None:
        raise NotFound(f"No loan with id {loan_id}.")
    return Loan.from_row(row)


def list_loans(
    conn: sqlite3.Connection,
    *,
    item_id: int | None = None,
    person_id: int | None = None,
    open_only: bool = False,
    overdue_only: bool = False,
    limit: int | None = None,
) -> list[Loan]:
    where: list[str] = []
    params: list[Any] = []
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if person_id is not None:
        where.append("person_id = ?")
        params.append(person_id)
    if open_only or overdue_only:
        where.append("returned_at IS NULL")
    if overdue_only:
        where.append("due_at IS NOT NULL AND due_at < ?")
        params.append(db.utcnow())

    sql = "SELECT * FROM loan_detail"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Open loans first, then most recent activity.
    sql += " ORDER BY (returned_at IS NULL) DESC, checked_out_at DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [Loan.from_row(r) for r in conn.execute(sql, params)]


def checkout(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    item_id: int,
    person_id: int | None = None,
    person_name: str | None = None,
    person_email: str | None = None,
    quantity: int = 1,
    due_at: str | None = None,
    note: str = "",
) -> Loan:
    """Lend ``quantity`` units of an item to a person.

    Identify the borrower either by ``person_id`` or by
    ``person_name``/``person_email`` -- the latter creates the person if they
    are new, which is what the checkout form does.

    The availability check and the INSERT happen inside one IMMEDIATE
    transaction, so two people racing for the last unit cannot both succeed.
    """
    quantity = _as_int(quantity, "Quantity", minimum=1)

    with db.transaction(conn):
        item = get_item(conn, item_id)
        if item.is_archived:
            raise ConflictError(f"{item.name} is archived and cannot be checked out.")

        if person_id is not None:
            person = get_person(conn, person_id)
        else:
            person = get_or_create_person(
                conn,
                actor=actor,
                name=_require(person_name, "Borrower name"),
                email=_clean_email(person_email),
            )

        if quantity > item.available:
            raise ConflictError(
                f"Only {item.available} of {item.quantity} "
                f"{'unit' if item.available == 1 else 'units'} of {item.name} "
                f"{'is' if item.available == 1 else 'are'} available; "
                f"{quantity} requested."
            )

        cur = conn.execute(
            """
            INSERT INTO loan (item_id, person_id, quantity, checked_out_at,
                              due_at, checkout_note, checked_out_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, person.id, quantity, db.utcnow(),
             _optional(due_at), _clean(note), str(actor)),
        )
        loan_id = int(cur.lastrowid)
        log_event(
            conn,
            actor=actor,
            action="loan.checkout",
            entity_type="loan",
            entity_id=loan_id,
            item_id=item_id,
            person_id=person.id,
            summary=(
                f"{person.name} checked out {quantity} x {item.name} "
                f"({item.available - quantity} of {item.quantity} left)"
            ),
            changes={
                "quantity": {"from": None, "to": quantity},
                "due_at": {"from": None, "to": _optional(due_at)},
            },
        )
        loan = get_loan(conn, loan_id)
    _notify_change()
    return loan


def return_loan(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    loan_id: int,
    quantity: int | None = None,
    note: str = "",
) -> Loan:
    """Return all (default) or part of a loan.

    A partial return closes the original loan and opens a residual loan for
    the units still held, linked by ``split_from_loan_id`` and keeping the
    original checkout date and due date. Loan rows are therefore never
    rewritten to a smaller quantity, and "how long has this been out" stays
    answerable after a partial return.

    Returns the loan row that was closed.
    """
    with db.transaction(conn):
        loan = get_loan(conn, loan_id)
        if not loan.is_open:
            raise ConflictError(
                f"That loan was already returned on {loan.returned_at}."
            )

        returning = loan.quantity if quantity is None else _as_int(
            quantity, "Quantity", minimum=1
        )
        if returning > loan.quantity:
            raise ConflictError(
                f"Cannot return {returning}: only {loan.quantity} "
                f"{'is' if loan.quantity == 1 else 'are'} out on this loan."
            )

        now = db.utcnow()
        conn.execute(
            "UPDATE loan SET returned_at = ?, returned_by = ?, return_note = ? "
            "WHERE id = ?",
            (now, str(actor), _clean(note), loan_id),
        )

        remaining = loan.quantity - returning
        residual_id: int | None = None
        if remaining > 0:
            cur = conn.execute(
                """
                INSERT INTO loan (item_id, person_id, quantity, checked_out_at,
                                  due_at, checkout_note, checked_out_by,
                                  split_from_loan_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (loan.item_id, loan.person_id, remaining, loan.checked_out_at,
                 loan.due_at, loan.checkout_note, loan.checked_out_by, loan_id),
            )
            residual_id = int(cur.lastrowid)

        action = "loan.partial_return" if remaining > 0 else "loan.return"
        summary = (
            f"{loan.person_name} returned {returning} of {loan.quantity} "
            f"x {loan.item_name}"
            if remaining > 0
            else f"{loan.person_name} returned {returning} x {loan.item_name}"
        )
        changes: dict[str, dict[str, Any]] = {
            "returned": {"from": 0, "to": returning},
            "still_out": {"from": loan.quantity, "to": remaining},
        }
        if residual_id is not None:
            changes["residual_loan_id"] = {"from": None, "to": residual_id}

        log_event(
            conn,
            actor=actor,
            action=action,
            entity_type="loan",
            entity_id=loan_id,
            item_id=loan.item_id,
            person_id=loan.person_id,
            summary=summary,
            changes=changes,
        )
        closed = get_loan(conn, loan_id)
    _notify_change()
    return closed


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def list_events(
    conn: sqlite3.Connection,
    *,
    item_id: int | None = None,
    person_id: int | None = None,
    action: str | None = None,
    actor: str | None = None,
    since: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Event]:
    """Read the audit log, newest first."""
    where: list[str] = []
    params: list[Any] = []
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if person_id is not None:
        where.append("person_id = ?")
        params.append(person_id)
    if action:
        where.append("action = ?")
        params.append(action)
    if actor:
        where.append("actor LIKE ?")
        params.append(f"%{actor}%")
    if since:
        where.append("at >= ?")
        params.append(since)

    sql = "SELECT * FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return [Event.from_row(r) for r in conn.execute(sql, params)]


def count_events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"])


def list_actions(conn: sqlite3.Connection) -> list[str]:
    """Distinct action names present in the log, for the history filter."""
    return [r["action"] for r in conn.execute(
        "SELECT DISTINCT action FROM event ORDER BY action"
    )]


# ---------------------------------------------------------------------------
# dashboard summary
# ---------------------------------------------------------------------------


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Headline numbers for the dashboard and the public page."""
    row = conn.execute(
        """
        SELECT COUNT(*)               AS item_count,
               COALESCE(SUM(quantity), 0)  AS total_units,
               COALESCE(SUM(out_qty), 0)   AS units_out,
               COALESCE(SUM(available), 0) AS units_available
        FROM item_status WHERE archived_at IS NULL
        """
    ).fetchone()
    low = conn.execute(
        "SELECT COUNT(*) AS n FROM item_status "
        "WHERE archived_at IS NULL AND min_quantity IS NOT NULL "
        "AND available <= min_quantity"
    ).fetchone()["n"]
    overdue = conn.execute(
        "SELECT COUNT(*) AS n FROM loan WHERE returned_at IS NULL "
        "AND due_at IS NOT NULL AND due_at < ?",
        (db.utcnow(),),
    ).fetchone()["n"]
    people = conn.execute("SELECT COUNT(*) AS n FROM person").fetchone()["n"]
    open_loans = conn.execute(
        "SELECT COUNT(*) AS n FROM loan WHERE returned_at IS NULL"
    ).fetchone()["n"]
    return {
        "item_count": row["item_count"],
        "total_units": row["total_units"],
        "units_out": row["units_out"],
        "units_available": row["units_available"],
        "low_stock_count": low,
        "overdue_count": overdue,
        "person_count": people,
        "open_loan_count": open_loans,
        "event_count": count_events(conn),
        "generated_at": db.utcnow(),
    }
