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

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import config, db, photos
from .models import (
    HOLD_STATE_LABELS,
    HOLD_STATES,
    Event,
    Hold,
    Item,
    Loan,
    Person,
    Photo,
    Unit,
)

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


# The fields covered by an event's hash, in this exact order. Changing this
# tuple invalidates every existing chain, so it is written out rather than
# derived from the table: a column added later must be added here deliberately,
# with a migration that rebuilds the chain.
_HASHED_FIELDS = (
    "id", "at", "actor", "action", "entity_type", "entity_id",
    "item_id", "person_id", "summary", "changes_json",
)

# Field separator for the digest. A control character, so it cannot occur in
# any of the values above -- otherwise an actor named "a\nb" could be crafted
# to produce the same digest as a different row.
_HASH_SEPARATOR = "\x1f"


def event_digest(row: Any, prev_hash: str) -> str:
    """The hash for one event row, given the hash of the row before it.

    Covers the row's own fields *and* its predecessor's digest, so the log is
    a chain: altering row 40 changes its hash, which invalidates row 41's
    prev_hash, and so on to the end of the table.
    """
    parts = [prev_hash]
    for field in _HASHED_FIELDS:
        value = row[field]
        parts.append("" if value is None else str(value))
    return hashlib.sha256(
        _HASH_SEPARATOR.join(parts).encode("utf-8")
    ).hexdigest()


def _head_hash(conn: sqlite3.Connection) -> str:
    """The hash of the most recent event, or '' when the log is empty.

    Safe to read inside a write transaction without a race: db.transaction()
    uses BEGIN IMMEDIATE, so this connection already holds the write lock and
    no other writer can append between this read and the insert that follows.
    """
    row = conn.execute(
        "SELECT hash FROM event ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return ""
    return row["hash"] or ""


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
    """Append one row to the audit log. Call only inside a transaction.

    The row is inserted, then hashed and updated in place. Two statements
    rather than one because the digest covers the row id, which SQLite only
    assigns at INSERT -- and including the id is what stops a deleted row
    being replaced by a forgery that reuses its position in the chain.
    """
    prev_hash = _head_hash(conn)
    cur = conn.execute(
        """
        INSERT INTO event (at, actor, action, entity_type, entity_id,
                           item_id, person_id, summary, changes_json,
                           prev_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            prev_hash,
        ),
    )
    event_id = int(cur.lastrowid)
    row = conn.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    conn.execute(
        "UPDATE event SET hash = ? WHERE id = ?",
        (event_digest(row, prev_hash), event_id),
    )
    return event_id


@dataclass(frozen=True, slots=True)
class ChainResult:
    """The outcome of walking the audit chain."""

    ok: bool
    checked: int
    head: str
    broken_at: int | None = None
    detail: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"{self.checked} event(s) verified, head {self.head[:12] or '-'}"
        return f"broken at event {self.broken_at}: {self.detail}"


def verify_audit_chain(conn: sqlite3.Connection) -> ChainResult:
    """Recompute every event hash and report the first row that disagrees.

    What this catches: a row edited, deleted or inserted directly in the
    database file -- the realistic threat here is not an outside attacker but
    someone with shell access quietly rewriting the record of a camera that
    never came back.

    What it does not catch: someone who edits a row *and* recomputes the whole
    chain from that point on, which anyone with write access can do. The
    defence against that is that the head hash is also written into the
    published inventory.json, into /health and into every nightly backup, so a
    convincing rewrite means finding and rewriting all of those too.
    """
    prev_hash = ""
    checked = 0
    for row in conn.execute("SELECT * FROM event ORDER BY id"):
        if row["hash"] is None:
            return ChainResult(
                False, checked, prev_hash, row["id"],
                "row has no hash (database predates the chain, or was rebuilt "
                "without it)",
            )
        if (row["prev_hash"] or "") != prev_hash:
            return ChainResult(
                False, checked, prev_hash, row["id"],
                "prev_hash does not match the preceding row -- an event was "
                "inserted, removed or reordered",
            )
        expected = event_digest(row, prev_hash)
        if expected != row["hash"]:
            return ChainResult(
                False, checked, prev_hash, row["id"],
                "contents do not match the stored hash -- this row was edited",
            )
        prev_hash = row["hash"]
        checked += 1
    return ChainResult(True, checked, prev_hash)


def audit_head(conn: sqlite3.Connection) -> str:
    """The current head of the audit chain, or '' if the log is empty."""
    return _head_hash(conn)


def rebuild_audit_chain(conn: sqlite3.Connection) -> int:
    """Recompute the whole chain from scratch. Returns the rows rewritten.

    Used once by the v2 -> v3 migration, to chain events written before the
    log had hashes at all. It is deliberately *not* exposed on the command
    line: running it after a tamper would erase the evidence, so the only
    caller is the migration, which runs when there is nothing to erase.
    """
    prev_hash = ""
    rewritten = 0
    for row in conn.execute("SELECT * FROM event ORDER BY id").fetchall():
        digest = event_digest(row, prev_hash)
        conn.execute(
            "UPDATE event SET prev_hash = ?, hash = ? WHERE id = ?",
            (prev_hash, digest, row["id"]),
        )
        prev_hash = digest
        rewritten += 1
    return rewritten


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


def list_people(
    conn: sqlite3.Connection,
    *,
    include_inactive: bool = False,
    include_merged: bool = False,
) -> list[Person]:
    """People, for pickers and the directory.

    Merged records are excluded separately from inactive ones. A merge
    deactivates the record too, so `include_inactive=True` alone would drag
    every merged duplicate back into the People page -- which is precisely the
    mess the merge was meant to clear up.
    """
    where: list[str] = []
    if not include_inactive:
        where.append("active = 1")
    if not include_merged:
        where.append("merged_into_id IS NULL")

    sql = "SELECT * FROM person"
    if where:
        sql += " WHERE " + " AND ".join(where)
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


def merge_people(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    keep_id: int,
    merge_id: int,
    reason: str = "",
) -> Person:
    """Fold one person record into another, keeping every trace of both.

    Two records for one human is the normal result of a stockroom that creates
    a borrower from whatever address was typed at the counter: alice@rit.edu
    one week and alice@g.rit.edu the next. Their history then reads as two
    people who each borrowed half as much, and the overdue chase goes to
    whichever address was used last.

    Nothing is deleted, in keeping with the rest of the system. Loans and the
    login account are repointed at the surviving record, the merged one is
    deactivated and marked with `merged_into_id`, and its audit events keep
    naming it -- history is what happened, not a tidied version of it.
    """
    if keep_id == merge_id:
        raise ValidationError("Those are the same person.")

    with db.transaction(conn):
        keep = get_person(conn, keep_id)
        merge = get_person(conn, merge_id)

        if merge.merged_into_id is not None:
            raise ConflictError(
                f"{merge.name} has already been merged into someone else."
            )
        if keep.merged_into_id is not None:
            raise ConflictError(
                f"{keep.name} is itself a merged record; merge into the "
                "surviving one instead."
            )

        loans = conn.execute(
            "SELECT COUNT(*) AS n FROM loan WHERE person_id = ?", (merge_id,)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE loan SET person_id = ? WHERE person_id = ?",
            (keep_id, merge_id),
        )
        # The login account, if there is one. An account points at a person;
        # leaving it pointing at a deactivated record would quietly break that
        # person's own "what do I have out" page.
        accounts_moved = conn.execute(
            "SELECT COUNT(*) AS n FROM account WHERE person_id = ?", (merge_id,)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE account SET person_id = ? WHERE person_id = ?",
            (keep_id, merge_id),
        )

        now = db.utcnow()
        conn.execute(
            "UPDATE person SET merged_into_id = ?, active = 0, updated_at = ? "
            "WHERE id = ?",
            (keep_id, now, merge_id),
        )

        # Keep the merged record's email in the survivor's notes: it is the
        # address that will keep turning up on old paperwork, and losing it
        # makes the merge irreversible in practice.
        note = f"Merged from {merge.name} <{merge.email}> on {now[:10]}"
        if _clean(reason):
            note += f" ({_clean(reason)})"
        combined = f"{keep.notes}\n{note}".strip() if keep.notes else note
        conn.execute(
            "UPDATE person SET notes = ?, updated_at = ? WHERE id = ?",
            (combined, now, keep_id),
        )

        summary = (
            f"Merged {merge.label} into {keep.label}: "
            f"{loans} loan(s) moved"
        )
        if accounts_moved:
            summary += f", {accounts_moved} account(s) repointed"
        log_event(
            conn,
            actor=actor,
            action="person.merge",
            entity_type="person",
            entity_id=merge_id,
            person_id=keep_id,
            summary=summary,
            changes={
                "merged_into_id": {"from": None, "to": keep_id},
                "loans_moved": {"from": None, "to": loans},
            },
        )
        return get_person(conn, keep_id)


def possible_duplicates(conn: sqlite3.Connection) -> list[tuple[Person, Person]]:
    """Pairs of people who look like the same human.

    Matched on name, case-insensitively, which catches the common case -- the
    same person entered under two addresses -- without pretending to be clever
    about it. Staff confirm; this only points.
    """
    rows = conn.execute(
        """
        SELECT a.id AS a_id, b.id AS b_id
        FROM person a
        JOIN person b
          ON a.name = b.name COLLATE NOCASE
         AND a.id < b.id
        WHERE a.merged_into_id IS NULL AND b.merged_into_id IS NULL
        ORDER BY a.name COLLATE NOCASE
        """
    ).fetchall()
    return [
        (get_person(conn, r["a_id"]), get_person(conn, r["b_id"])) for r in rows
    ]


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

_ITEM_FIELDS = (
    "name", "description", "product_url", "quantity",
    "unit", "shelf", "sub_location", "min_quantity", "barcode", "tracked",
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
    only_held: bool = False,
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
    if only_held:
        where.append("held_qty > 0")

    sql = "SELECT * FROM item_status"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name COLLATE NOCASE"
    return [Item.from_row(r) for r in conn.execute(sql, params)]


def list_storage_units(conn: sqlite3.Connection) -> list[str]:
    """Distinct storage units -- cabinets -- for filter dropdowns and datalists.

    Named the long way round because "unit" is overloaded in this domain:
    `item.unit` is a cabinet, and the `unit` table is one individual physical
    thing. Two functions called list_units silently shadowed each other, and
    the filter dropdown quietly went empty; hence the explicit names, and
    test_the_two_kinds_of_unit_have_separate_functions.
    """
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
    tracked: int | bool = False,
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
                              unit, shelf, sub_location, min_quantity, tracked,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (code, name, _clean(description), _optional(product_url), quantity,
             _clean(unit), _clean(shelf), _optional(sub_location), min_quantity,
             1 if tracked else 0, now, now),
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
        if "tracked" in updates:
            changed["tracked"] = 1 if updates["tracked"] else 0
        if "quantity" in updates:
            new_qty = _as_int(updates["quantity"], "Quantity", minimum=0)
            # The one invariant that cannot be expressed as a CHECK constraint:
            # total owned may never fall below what is already spoken for --
            # units on loan, plus units held out of service. Dropping below
            # that would make item_status report negative availability.
            spoken_for = current.out_qty + current.held_qty
            if new_qty < spoken_for:
                raise ConflictError(
                    f"Cannot set quantity to {new_qty}: {current.out_qty} "
                    f"{'unit is' if current.out_qty == 1 else 'units are'} "
                    f"checked out and {current.held_qty} "
                    f"{'is' if current.held_qty == 1 else 'are'} out of service."
                    if current.held_qty
                    else
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
# units and condition
#
# Two questions the count-only model could not answer: *which* one, and *what
# state is it in*. A `unit` row is one physical thing; a `item_hold` row says
# some units are not lendable and why.
#
# Availability stays derived. Nothing here writes an availability number --
# item_status subtracts open holds, exactly as it subtracts open loans.
# ---------------------------------------------------------------------------


def get_unit(conn: sqlite3.Connection, unit_id: int) -> Unit:
    row = conn.execute(
        "SELECT * FROM unit_status WHERE id = ?", (unit_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"No unit with id {unit_id}.")
    return Unit.from_row(row)


def get_unit_by_asset_tag(conn: sqlite3.Connection, asset_tag: str) -> Unit | None:
    tag = _clean(asset_tag)
    if not tag:
        return None
    row = conn.execute(
        "SELECT * FROM unit_status WHERE asset_tag = ?", (tag,)
    ).fetchone()
    return Unit.from_row(row) if row else None


def list_units(
    conn: sqlite3.Connection,
    *,
    item_id: int | None = None,
    include_retired: bool = False,
    state: str | None = None,
) -> list[Unit]:
    where: list[str] = []
    params: list[Any] = []
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if not include_retired:
        where.append("retired_at IS NULL")
    if state:
        where.append("state = ?")
        params.append(state)

    sql = "SELECT * FROM unit_status"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(asset_tag, serial, CAST(id AS TEXT))"
    return [Unit.from_row(r) for r in conn.execute(sql, params)]


def create_unit(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    item_id: int,
    asset_tag: str | None = None,
    serial: str | None = None,
    note: str = "",
) -> Unit:
    """Register one individual unit of an item.

    Deliberately does not touch ``item.quantity``. The two are allowed to
    disagree while somebody is part-way through tagging a shelf of cameras,
    and a unit count that silently rewrote the owned count would make that
    half-finished state destructive. ``stockroom doctor`` reports the gap.
    """
    with db.transaction(conn):
        item = get_item(conn, item_id)
        tag = _optional(asset_tag)
        if tag is not None:
            clash = get_unit_by_asset_tag(conn, tag)
            if clash is not None:
                raise ConflictError(
                    f"Asset tag {tag} already belongs to {clash.item_name}."
                )
        now = db.utcnow()
        cur = conn.execute(
            """
            INSERT INTO unit (item_id, asset_tag, serial, note,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, tag, _optional(serial), _clean(note), now, now),
        )
        unit_id = int(cur.lastrowid)
        log_event(
            conn,
            actor=actor,
            action="unit.create",
            entity_type="unit",
            entity_id=unit_id,
            item_id=item_id,
            summary=f"Registered {tag or f'unit #{unit_id}'} of {item.name}",
            changes={
                "asset_tag": {"from": None, "to": tag},
                "serial": {"from": None, "to": _optional(serial)},
            },
        )
        unit = get_unit(conn, unit_id)
    _notify_change()
    return unit


_UNIT_FIELDS = ("asset_tag", "serial", "note")


def update_unit(
    conn: sqlite3.Connection, *, actor: Actor, unit_id: int, **updates: Any
) -> Unit:
    """Correct a unit's asset tag, serial or note."""
    unknown = set(updates) - set(_UNIT_FIELDS)
    if unknown:
        raise ValidationError(f"Unknown unit field(s): {', '.join(sorted(unknown))}")

    with db.transaction(conn):
        current = get_unit(conn, unit_id)
        changed: dict[str, Any] = {}
        if "asset_tag" in updates:
            tag = _optional(updates["asset_tag"])
            if tag is not None:
                clash = get_unit_by_asset_tag(conn, tag)
                if clash is not None and clash.id != unit_id:
                    raise ConflictError(
                        f"Asset tag {tag} already belongs to {clash.item_name}."
                    )
            changed["asset_tag"] = tag
        if "serial" in updates:
            changed["serial"] = _optional(updates["serial"])
        if "note" in updates:
            changed["note"] = _clean(updates["note"])

        changes = _diff(current.as_dict(), changed)
        if not changes:
            return current

        changed["updated_at"] = db.utcnow()
        assignments = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE unit SET {assignments} WHERE id = ?",
            (*changed.values(), unit_id),
        )
        log_event(
            conn,
            actor=actor,
            action="unit.update",
            entity_type="unit",
            entity_id=unit_id,
            item_id=current.item_id,
            summary=f"Updated {current.label} ({_summarize_fields(changes)})",
            changes=changes,
        )
        unit = get_unit(conn, unit_id)
    _notify_change()
    return unit


def retire_unit(
    conn: sqlite3.Connection, *, actor: Actor, unit_id: int, reason: str = ""
) -> Unit:
    """Stop tracking a unit. The row stays, so its loan history still reads."""
    with db.transaction(conn):
        unit = get_unit(conn, unit_id)
        if unit.is_retired:
            raise ConflictError(f"{unit.label} is already retired.")
        now = db.utcnow()
        conn.execute(
            "UPDATE unit SET retired_at = ?, updated_at = ? WHERE id = ?",
            (now, now, unit_id),
        )
        summary = f"Retired {unit.label} of {unit.item_name}"
        if _clean(reason):
            summary += f" ({_clean(reason)})"
        log_event(
            conn,
            actor=actor,
            action="unit.retire",
            entity_type="unit",
            entity_id=unit_id,
            item_id=unit.item_id,
            summary=summary,
            changes={"retired_at": {"from": None, "to": now}},
        )
        retired = get_unit(conn, unit_id)
    _notify_change()
    return retired


# ---------------------------------------------------------------------------
# holds -- "this one is broken"
# ---------------------------------------------------------------------------


def get_hold(conn: sqlite3.Connection, hold_id: int) -> Hold:
    row = conn.execute(
        "SELECT * FROM hold_detail WHERE id = ?", (hold_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"No hold with id {hold_id}.")
    return Hold.from_row(row)


def list_holds(
    conn: sqlite3.Connection,
    *,
    item_id: int | None = None,
    open_only: bool = True,
    unaccounted_only: bool = False,
    limit: int | None = None,
) -> list[Hold]:
    where: list[str] = []
    params: list[Any] = []
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if open_only:
        where.append("closed_at IS NULL")
    if unaccounted_only:
        where.append("state IN ('missing', 'gone')")

    sql = "SELECT * FROM hold_detail"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (closed_at IS NULL) DESC, opened_at DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [Hold.from_row(r) for r in conn.execute(sql, params)]


def _validate_state(state: str) -> str:
    state = _clean(state).lower()
    if state not in HOLD_STATES:
        raise ValidationError(
            f"{state!r} is not a condition. Use one of: {', '.join(HOLD_STATES)}."
        )
    return state


def open_hold(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    item_id: int,
    state: str,
    quantity: int = 1,
    unit_id: int | None = None,
    note: str = "",
    loan_id: int | None = None,
) -> Hold:
    """Take units out of service: broken, in repair, missing or written off.

    Availability drops by ``quantity``; ``item.quantity`` does not move. The
    stockroom still owns ten of them, and saying so is the difference between
    "we have nine" and "we bought ten and one is unaccounted for".

    For a tracked item pass ``unit_id`` and the hold covers that one physical
    thing. For a countable item pass a quantity.
    """
    with db.transaction(conn):
        hold = _open_hold_locked(
            conn, actor=actor, item_id=item_id, state=state, quantity=quantity,
            unit_id=unit_id, note=note, loan_id=loan_id,
        )
    _notify_change()
    return hold


def _open_hold_locked(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    item_id: int,
    state: str,
    quantity: int = 1,
    unit_id: int | None = None,
    note: str = "",
    loan_id: int | None = None,
) -> Hold:
    """One hold, assuming a transaction is already open. See open_hold.

    Separate so return_loan can close a loan and quarantine what came back in
    a single transaction -- a damaged return must not be able to half-happen.
    """
    state = _validate_state(state)
    quantity = _as_int(quantity, "Quantity", minimum=1)

    item = get_item(conn, item_id)

    if unit_id is not None:
        unit = get_unit(conn, unit_id)
        if unit.item_id != item_id:
            raise ValidationError(
                f"{unit.label} does not belong to {item.name}."
            )
        if unit.state != "ok":
            raise ConflictError(
                f"{unit.label} is already recorded as "
                f"{unit.state_label.lower()}."
            )
        quantity = 1

    # Availability is checked against what is on the shelf, not what is
    # owned: units already lent out cannot simultaneously be in the repair
    # pile. A camera that comes back broken is held on return, by which
    # point its loan is closed and the unit is back on the shelf.
    if quantity > item.available:
        raise ConflictError(
            f"Only {item.available} of {item.name} "
            f"{'is' if item.available == 1 else 'are'} on the shelf; "
            f"cannot put {quantity} out of service."
        )

    cur = conn.execute(
        """
        INSERT INTO item_hold (item_id, unit_id, quantity, state, note,
                               loan_id, opened_at, opened_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, unit_id, quantity, state, _clean(note), loan_id,
         db.utcnow(), str(actor)),
    )
    hold_id = int(cur.lastrowid)
    hold = get_hold(conn, hold_id)
    summary = (
        f"{hold.what} marked {hold.state_label.lower()}"
        f" ({item.available - quantity} of {item.quantity} now available)"
    )
    if _clean(note):
        summary += f": {_clean(note)}"
    log_event(
        conn,
        actor=actor,
        action="item.hold_open",
        entity_type="hold",
        entity_id=hold_id,
        item_id=item_id,
        summary=summary,
        changes={
            "state": {"from": "ok", "to": state},
            "quantity": {"from": None, "to": quantity},
        },
    )
    return get_hold(conn, hold_id)


def change_hold(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    hold_id: int,
    state: str,
    note: str = "",
) -> Hold:
    """Move a hold along: broken -> in repair -> written off, or back again.

    Availability does not change -- the units were already off the shelf and
    they still are. What changes is what the stockroom expects to happen next,
    which is the whole reason these are separate states.
    """
    state = _validate_state(state)
    with db.transaction(conn):
        hold = get_hold(conn, hold_id)
        if not hold.is_open:
            raise ConflictError(
                f"That hold was closed on {hold.closed_at}; open a new one."
            )
        if hold.state == state:
            return hold

        changes = {"state": {"from": hold.state, "to": state}}
        fields: dict[str, Any] = {"state": state}
        if _clean(note):
            fields["note"] = _clean(note)
            changes["note"] = {"from": hold.note, "to": _clean(note)}

        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE item_hold SET {assignments} WHERE id = ?",
            (*fields.values(), hold_id),
        )
        log_event(
            conn,
            actor=actor,
            action="item.hold_change",
            entity_type="hold",
            entity_id=hold_id,
            item_id=hold.item_id,
            summary=(
                f"{hold.what}: {hold.state_label.lower()} -> "
                f"{HOLD_STATE_LABELS[state].lower()}"
            ),
            changes=changes,
        )
        updated = get_hold(conn, hold_id)
    _notify_change()
    return updated


def close_hold(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    hold_id: int,
    resolution: str = "",
) -> Hold:
    """Put the units back on the shelf. Availability rises again."""
    with db.transaction(conn):
        hold = get_hold(conn, hold_id)
        if not hold.is_open:
            raise ConflictError(f"That hold was already closed on {hold.closed_at}.")

        now = db.utcnow()
        conn.execute(
            "UPDATE item_hold SET closed_at = ?, closed_by = ?, resolution = ? "
            "WHERE id = ?",
            (now, str(actor), _clean(resolution), hold_id),
        )
        summary = f"{hold.what} back in service (was {hold.state_label.lower()})"
        if _clean(resolution):
            summary += f": {_clean(resolution)}"
        log_event(
            conn,
            actor=actor,
            action="item.hold_close",
            entity_type="hold",
            entity_id=hold_id,
            item_id=hold.item_id,
            summary=summary,
            changes={"state": {"from": hold.state, "to": "ok"}},
        )
        closed = get_hold(conn, hold_id)
    _notify_change()
    return closed


# ---------------------------------------------------------------------------
# photos
# ---------------------------------------------------------------------------


def list_photos(conn: sqlite3.Connection, item_id: int) -> list[Photo]:
    rows = conn.execute(
        "SELECT * FROM item_photo WHERE item_id = ? AND deleted_at IS NULL "
        "ORDER BY is_primary DESC, id",
        (item_id,),
    )
    return [Photo.from_row(r) for r in rows]


def get_photo(conn: sqlite3.Connection, photo_id: int) -> Photo:
    row = conn.execute(
        "SELECT * FROM item_photo WHERE id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"No photo with id {photo_id}.")
    return Photo.from_row(row)


def primary_photos(conn: sqlite3.Connection) -> dict[int, str]:
    """item_id -> filename, for showing thumbnails on a list of items."""
    rows = conn.execute(
        "SELECT item_id, filename FROM item_photo "
        "WHERE deleted_at IS NULL AND is_primary = 1"
    )
    return {r["item_id"]: r["filename"] for r in rows}


def add_photo(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    item_id: int,
    data: bytes,
    caption: str = "",
) -> Photo:
    """Store an uploaded photo against an item.

    The file is written before the transaction opens, because writing it is
    the slow part and holding the database's write lock across a disk write is
    how a busy counter starts timing out. If the row then fails to insert the
    file is removed again; an orphaned file is harmless anyway, since nothing
    serves a file that has no row.
    """
    stored = photos.store(data)
    try:
        with db.transaction(conn):
            item = get_item(conn, item_id)
            existing = list_photos(conn, item_id)
            now = db.utcnow()
            cur = conn.execute(
                """
                INSERT INTO item_photo (item_id, filename, caption, is_primary,
                                        width, height, bytes, created_at,
                                        created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, stored.filename, _clean(caption),
                 0 if existing else 1,      # the first photo leads by default
                 stored.width, stored.height, stored.bytes, now, str(actor)),
            )
            photo_id = int(cur.lastrowid)
            log_event(
                conn,
                actor=actor,
                action="item.photo_add",
                entity_type="item",
                entity_id=item_id,
                item_id=item_id,
                summary=f"Added a photo of {item.name}",
                changes={"photo": {"from": None, "to": stored.filename}},
            )
            return get_photo(conn, photo_id)
    except Exception:
        photos.delete_file(stored.filename)
        raise


def set_primary_photo(
    conn: sqlite3.Connection, *, actor: Actor, photo_id: int
) -> Photo:
    """Choose which photo represents the item in lists and at the counter."""
    with db.transaction(conn):
        photo = get_photo(conn, photo_id)
        if photo.deleted_at is not None:
            raise ConflictError("That photo was removed.")
        if photo.is_primary:
            return photo
        item = get_item(conn, photo.item_id)
        # Clear the old one first: a partial unique index allows only one.
        conn.execute(
            "UPDATE item_photo SET is_primary = 0 WHERE item_id = ?",
            (photo.item_id,),
        )
        conn.execute(
            "UPDATE item_photo SET is_primary = 1 WHERE id = ?", (photo_id,)
        )
        log_event(
            conn,
            actor=actor,
            action="item.photo_primary",
            entity_type="item",
            entity_id=photo.item_id,
            item_id=photo.item_id,
            summary=f"Changed the main photo of {item.name}",
        )
        return get_photo(conn, photo_id)


def remove_photo(
    conn: sqlite3.Connection, *, actor: Actor, photo_id: int
) -> Photo:
    """Hide a photo. The file is left on disk, so a mis-click is recoverable."""
    with db.transaction(conn):
        photo = get_photo(conn, photo_id)
        if photo.deleted_at is not None:
            raise ConflictError("That photo was already removed.")
        item = get_item(conn, photo.item_id)
        conn.execute(
            "UPDATE item_photo SET deleted_at = ?, is_primary = 0 WHERE id = ?",
            (db.utcnow(), photo_id),
        )
        # Promote another one, so an item does not silently lose its thumbnail.
        remaining = list_photos(conn, photo.item_id)
        if remaining and not any(p.is_primary for p in remaining):
            conn.execute(
                "UPDATE item_photo SET is_primary = 1 WHERE id = ?",
                (remaining[0].id,),
            )
        log_event(
            conn,
            actor=actor,
            action="item.photo_remove",
            entity_type="item",
            entity_id=photo.item_id,
            item_id=photo.item_id,
            summary=f"Removed a photo of {item.name}",
        )
        return get_photo(conn, photo_id)


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
    with db.transaction(conn):
        loan = _checkout_locked(
            conn, actor=actor, item_id=item_id, person_id=person_id,
            person_name=person_name, person_email=person_email,
            quantity=quantity, due_at=due_at, note=note,
        )
    _notify_change()
    return loan


def _checkout_locked(
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
    """One checkout, assuming a transaction is already open.

    Split out so a basket of items can be checked out inside a single
    transaction: the whole basket commits or none of it does, and the publish
    notification fires once at the end rather than once per line.
    """
    quantity = _as_int(quantity, "Quantity", minimum=1)

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
            f"{'unit' if item.quantity == 1 else 'units'} of {item.name} "
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
    return get_loan(conn, loan_id)


@dataclass(frozen=True, slots=True)
class BasketLine:
    """One line of a counter basket: how many of which item."""

    item_id: int
    quantity: int = 1


def checkout_many(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    lines: list[BasketLine] | list[tuple[int, int]],
    person_id: int | None = None,
    person_name: str | None = None,
    person_email: str | None = None,
    due_at: str | None = None,
    note: str = "",
) -> list[Loan]:
    """Check out a whole basket to one person, all or nothing.

    A shoot takes a body, a lens, a tripod, batteries and cards. Doing that as
    five separate checkouts means five chances to be interrupted half way and
    leave the record disagreeing with what walked out of the room. One
    transaction means the basket either happens or it does not.

    Each line still writes its own ``loan.checkout`` event -- per-item history
    is what the audit log is for -- plus one ``loan.checkout_batch`` summary,
    so the timeline reads as one visit to the counter rather than five
    unrelated events a second apart.
    """
    basket = [
        line if isinstance(line, BasketLine) else BasketLine(line[0], line[1])
        for line in lines
    ]
    if not basket:
        raise ValidationError("There is nothing in the basket.")

    with db.transaction(conn):
        # Resolve the borrower once, so a basket for somebody new does not
        # create them five times over (and so the failure, if the email is
        # bad, happens before any loan is written).
        if person_id is None:
            person = get_or_create_person(
                conn,
                actor=actor,
                name=_require(person_name, "Borrower name"),
                email=_clean_email(person_email),
            )
            person_id = person.id
        else:
            person = get_person(conn, person_id)

        loans = [
            _checkout_locked(
                conn, actor=actor, item_id=line.item_id, person_id=person_id,
                quantity=line.quantity, due_at=due_at, note=note,
            )
            for line in basket
        ]

        total = sum(loan.quantity for loan in loans)
        log_event(
            conn,
            actor=actor,
            action="loan.checkout_batch",
            entity_type="person",
            entity_id=person_id,
            person_id=person_id,
            summary=(
                f"{person.name} checked out {total} unit(s) across "
                f"{len(loans)} item(s): "
                + ", ".join(f"{l.quantity} x {l.item_name}" for l in loans)
            ),
            changes={"loan_ids": {"from": None, "to": [l.id for l in loans]}},
        )
    _notify_change()
    return loans


def return_many(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    loan_ids: list[int],
    note: str = "",
) -> list[Loan]:
    """Close a set of loans in one transaction. The mirror of checkout_many."""
    if not loan_ids:
        raise ValidationError("There is nothing to return.")

    with db.transaction(conn):
        closed = [
            _return_locked(conn, actor=actor, loan_id=loan_id, note=note)
            for loan_id in loan_ids
        ]
        person_id = closed[0].person_id
        log_event(
            conn,
            actor=actor,
            action="loan.return_batch",
            entity_type="person",
            entity_id=person_id,
            person_id=person_id,
            summary=(
                f"{closed[0].person_name} returned {len(closed)} item(s): "
                + ", ".join(f"{l.quantity} x {l.item_name}" for l in closed)
            ),
            changes={"loan_ids": {"from": [l.id for l in closed], "to": None}},
        )
    _notify_change()
    return closed


def open_loans_for_item_and_person(
    conn: sqlite3.Connection, *, item_id: int, person_id: int
) -> list[Loan]:
    """Open loans of one item held by one person, oldest first.

    The counter's return scan uses this: somebody hands back a tripod, and the
    question is which of their open loans that closes. Oldest first, because
    that is the one most likely to be overdue.
    """
    rows = conn.execute(
        "SELECT * FROM loan_detail WHERE item_id = ? AND person_id = ? "
        "AND returned_at IS NULL ORDER BY checked_out_at, id",
        (item_id, person_id),
    )
    return [Loan.from_row(r) for r in rows]


def return_loan(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    loan_id: int,
    quantity: int | None = None,
    note: str = "",
    condition: str | None = None,
    condition_quantity: int | None = None,
    unit_id: int | None = None,
) -> Loan:
    """Return all (default) or part of a loan.

    A partial return closes the original loan and opens a residual loan for
    the units still held, linked by ``split_from_loan_id`` and keeping the
    original checkout date and due date. Loan rows are therefore never
    rewritten to a smaller quantity, and "how long has this been out" stays
    answerable after a partial return.

    ``condition`` records that some or all of what came back is not fit to
    lend again -- 'broken', 'repair', 'missing' or 'gone'. It opens a hold in
    the same transaction, linked to this loan, which is what makes "who had it
    when it broke" answerable rather than just "when did it break". Doing it
    here rather than as a second step is not a shortcut: at a counter, the
    moment someone hands back a dented lens is the only moment anybody will
    reliably write it down.

    Returns the loan row that was closed.
    """
    with db.transaction(conn):
        closed = _return_locked(
            conn, actor=actor, loan_id=loan_id, quantity=quantity, note=note,
            condition=condition, condition_quantity=condition_quantity,
            unit_id=unit_id,
        )
    _notify_change()
    return closed


def _return_locked(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    loan_id: int,
    quantity: int | None = None,
    note: str = "",
    condition: str | None = None,
    condition_quantity: int | None = None,
    unit_id: int | None = None,
) -> Loan:
    """One return, assuming a transaction is already open. See return_loan."""
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

    if condition:
        # Opened after the return is recorded, so the units are back on the
        # shelf and open_hold's availability check sees them. The order
        # matters: hold first and it would refuse, because the units it is
        # about to quarantine are still counted as lent out.
        damaged = (
            returning if condition_quantity is None
            else _as_int(condition_quantity, "Damaged quantity", minimum=1)
        )
        if damaged > returning:
            raise ConflictError(
                f"Cannot mark {damaged} as {condition}: only {returning} "
                f"{'was' if returning == 1 else 'were'} returned."
            )
        _open_hold_locked(
            conn,
            actor=actor,
            item_id=loan.item_id,
            state=condition,
            quantity=damaged,
            unit_id=unit_id,
            note=_clean(note),
            loan_id=loan_id,
        )

    return get_loan(conn, loan_id)


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
        SELECT COUNT(*)                        AS item_count,
               COALESCE(SUM(quantity), 0)        AS total_units,
               COALESCE(SUM(out_qty), 0)         AS units_out,
               COALESCE(SUM(available), 0)       AS units_available,
               COALESCE(SUM(held_qty), 0)        AS units_held,
               COALESCE(SUM(unaccounted_qty), 0) AS units_unaccounted
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
        # Out of service: broken or in repair (units_held includes these plus
        # the unaccounted ones), versus nobody knows where it is.
        "units_held": row["units_held"],
        "units_unaccounted": row["units_unaccounted"],
        "low_stock_count": low,
        "overdue_count": overdue,
        "person_count": people,
        "open_loan_count": open_loans,
        "event_count": count_events(conn),
        "generated_at": db.utcnow(),
    }
