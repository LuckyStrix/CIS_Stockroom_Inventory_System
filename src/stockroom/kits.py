"""Kits: named bundles of items that go out together.

A shoot takes a body, a lens, two batteries and a card. Staff think in kits;
the loan table thinks in items. This module is the translation, and it is
deliberately thin: a kit is expanded into ordinary basket lines at the counter
and then forgotten.

Nothing is lent "as a kit". There is no kit loan, no kit state, and no way for
a kit to disagree with the loans it produced -- which is the failure mode of
every inventory system that models bundles as first-class borrowable things.
Change a kit's contents tomorrow and yesterday's loans are unaffected, because
they were only ever loans of items.

Same rule as everywhere else: every mutation here writes its `event` row in
the same transaction as the change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db
from .service import (
    MAX_TEXT,
    Actor,
    ConflictError,
    NotFound,
    ValidationError,
    _bounded,
    _clean,
    _require,
    get_item,
    log_event,
)


@dataclass(frozen=True, slots=True)
class KitLine:
    """One line of a kit: how many of which item."""

    kit_id: int
    item_id: int
    quantity: int
    item_name: str = ""
    item_barcode: str | None = None
    item_unit: str = ""
    item_shelf: str = ""
    item_available: int = 0
    item_archived_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> KitLine:
        known = cls.__slots__
        return cls(**{k: row[k] for k in row.keys() if k in known})

    @property
    def is_satisfiable(self) -> bool:
        """Whether this line could go out right now."""
        return self.item_archived_at is None and self.item_available >= self.quantity

    @property
    def shortfall(self) -> int:
        return max(0, self.quantity - self.item_available)


@dataclass(frozen=True, slots=True)
class Kit:
    id: int
    name: str
    description: str
    created_at: str
    updated_at: str
    archived_at: str | None
    lines: tuple[KitLine, ...] = ()

    @classmethod
    def from_row(cls, row: Any, lines: tuple[KitLine, ...] = ()) -> Kit:
        known = {"id", "name", "description", "created_at", "updated_at",
                 "archived_at"}
        return cls(**{k: row[k] for k in row.keys() if k in known}, lines=lines)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_complete(self) -> bool:
        """Every line is on the shelf, so the whole kit can go out."""
        return bool(self.lines) and all(l.is_satisfiable for l in self.lines)

    @property
    def missing(self) -> list[KitLine]:
        return [l for l in self.lines if not l.is_satisfiable]

    @property
    def total_units(self) -> int:
        return sum(l.quantity for l in self.lines)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_kit(conn: sqlite3.Connection, kit_id: int) -> Kit:
    row = conn.execute("SELECT * FROM kit WHERE id = ?", (kit_id,)).fetchone()
    if row is None:
        raise NotFound(f"No kit with id {kit_id}.")
    return Kit.from_row(row, _lines(conn, kit_id))


def _lines(conn: sqlite3.Connection, kit_id: int) -> tuple[KitLine, ...]:
    rows = conn.execute(
        "SELECT * FROM kit_contents WHERE kit_id = ? "
        "ORDER BY item_name COLLATE NOCASE",
        (kit_id,),
    )
    return tuple(KitLine.from_row(r) for r in rows)


def list_kits(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Kit]:
    sql = "SELECT * FROM kit"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY name COLLATE NOCASE"
    return [Kit.from_row(r, _lines(conn, r["id"])) for r in conn.execute(sql)]


def kits_containing(conn: sqlite3.Connection, item_id: int) -> list[Kit]:
    """Which kits an item belongs to -- shown on the item page.

    Worth surfacing before someone archives a camera that four kits depend on.
    """
    rows = conn.execute(
        "SELECT k.* FROM kit k JOIN kit_item ki ON ki.kit_id = k.id "
        "WHERE ki.item_id = ? AND k.archived_at IS NULL "
        "ORDER BY k.name COLLATE NOCASE",
        (item_id,),
    )
    return [Kit.from_row(r, _lines(conn, r["id"])) for r in rows]


def basket_lines(kit: Kit) -> list[tuple[int, int]]:
    """The kit as ``(item_id, quantity)`` pairs for service.checkout_many."""
    return [(line.item_id, line.quantity) for line in kit.lines]


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def create_kit(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    name: str,
    description: str = "",
) -> Kit:
    """Create an empty kit. Contents are set separately."""
    name = _require(name, "Kit name")
    with db.transaction(conn):
        clash = conn.execute(
            "SELECT id FROM kit WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if clash is not None:
            raise ConflictError(f"There is already a kit called {name}.")

        now = db.utcnow()
        cur = conn.execute(
            "INSERT INTO kit (name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, _bounded(description, "Description", MAX_TEXT), now, now),
        )
        kit_id = int(cur.lastrowid)
        log_event(
            conn,
            actor=actor,
            action="kit.create",
            entity_type="kit",
            entity_id=kit_id,
            summary=f"Created the kit {name}",
        )
        return get_kit(conn, kit_id)


def update_kit(
    conn: sqlite3.Connection, *, actor: Actor, kit_id: int, **updates: Any
) -> Kit:
    """Rename a kit or change its description."""
    unknown = set(updates) - {"name", "description"}
    if unknown:
        raise ValidationError(f"Unknown kit field(s): {', '.join(sorted(unknown))}")

    with db.transaction(conn):
        kit = get_kit(conn, kit_id)
        changed: dict[str, Any] = {}
        if "name" in updates:
            name = _require(updates["name"], "Kit name")
            clash = conn.execute(
                "SELECT id FROM kit WHERE name = ? COLLATE NOCASE AND id <> ?",
                (name, kit_id),
            ).fetchone()
            if clash is not None:
                raise ConflictError(f"There is already a kit called {name}.")
            if name != kit.name:
                changed["name"] = name
        if "description" in updates:
            description = _clean(updates["description"])
            if description != kit.description:
                changed["description"] = description

        if not changed:
            return kit

        changes = {k: {"from": getattr(kit, k), "to": v} for k, v in changed.items()}
        changed["updated_at"] = db.utcnow()
        assignments = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE kit SET {assignments} WHERE id = ?", (*changed.values(), kit_id)
        )
        log_event(
            conn,
            actor=actor,
            action="kit.update",
            entity_type="kit",
            entity_id=kit_id,
            summary=f"Updated the kit {kit.name}",
            changes=changes,
        )
        return get_kit(conn, kit_id)


def set_kit_contents(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    kit_id: int,
    lines: list[tuple[int, int]] | dict[int, int],
) -> Kit:
    """Replace a kit's contents wholesale.

    Replace rather than add/remove because the editing UI is a list of lines
    the user has just finished arranging: applying it as a diff would make the
    result depend on what was there before, which is not what the person
    clicking Save is picturing.

    Changing a kit never touches loans that came from it. A kit is a shortcut
    for filling a basket, not a record of anything that happened.
    """
    pairs = list(lines.items()) if isinstance(lines, dict) else list(lines)

    with db.transaction(conn):
        kit = get_kit(conn, kit_id)
        if kit.is_archived:
            raise ConflictError(f"{kit.name} is archived; restore it first.")

        resolved: dict[int, int] = {}
        for item_id, quantity in pairs:
            quantity = int(quantity)
            if quantity <= 0:
                continue        # a zeroed line is how the form removes one
            item = get_item(conn, item_id)      # raises NotFound if bogus
            if item.is_archived:
                raise ConflictError(
                    f"{item.name} is archived and cannot be part of a kit."
                )
            resolved[item_id] = quantity

        before = {l.item_id: l.quantity for l in kit.lines}
        if before == resolved:
            return kit

        conn.execute("DELETE FROM kit_item WHERE kit_id = ?", (kit_id,))
        conn.executemany(
            "INSERT INTO kit_item (kit_id, item_id, quantity) VALUES (?, ?, ?)",
            [(kit_id, item_id, qty) for item_id, qty in resolved.items()],
        )
        conn.execute(
            "UPDATE kit SET updated_at = ? WHERE id = ?", (db.utcnow(), kit_id)
        )

        updated = get_kit(conn, kit_id)
        log_event(
            conn,
            actor=actor,
            action="kit.contents",
            entity_type="kit",
            entity_id=kit_id,
            summary=(
                f"{kit.name} now contains {len(resolved)} item(s), "
                f"{updated.total_units} unit(s) in total"
            ),
            changes={"contents": {"from": before, "to": resolved}},
        )
        return updated


def archive_kit(
    conn: sqlite3.Connection, *, actor: Actor, kit_id: int
) -> Kit:
    """Retire a kit. Its rows stay, so the history still reads."""
    with db.transaction(conn):
        kit = get_kit(conn, kit_id)
        if kit.is_archived:
            raise ConflictError(f"{kit.name} is already archived.")
        now = db.utcnow()
        conn.execute(
            "UPDATE kit SET archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, kit_id),
        )
        log_event(
            conn,
            actor=actor,
            action="kit.archive",
            entity_type="kit",
            entity_id=kit_id,
            summary=f"Archived the kit {kit.name}",
            changes={"archived_at": {"from": None, "to": now}},
        )
        return get_kit(conn, kit_id)


def restore_kit(conn: sqlite3.Connection, *, actor: Actor, kit_id: int) -> Kit:
    with db.transaction(conn):
        kit = get_kit(conn, kit_id)
        if not kit.is_archived:
            raise ConflictError(f"{kit.name} is not archived.")
        conn.execute(
            "UPDATE kit SET archived_at = NULL, updated_at = ? WHERE id = ?",
            (db.utcnow(), kit_id),
        )
        log_event(
            conn,
            actor=actor,
            action="kit.restore",
            entity_type="kit",
            entity_id=kit_id,
            summary=f"Restored the kit {kit.name}",
            changes={"archived_at": {"from": kit.archived_at, "to": None}},
        )
        return get_kit(conn, kit_id)
