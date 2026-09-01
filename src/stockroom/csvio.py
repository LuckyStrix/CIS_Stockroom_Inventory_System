"""CSV import and export.

Import exists to bulk-load whatever spreadsheet the stockroom is tracked in
today. It is deliberately conservative:

* **Dry run by default at the CLI.** ``stockroom import file.csv`` reports
  what it *would* do and changes nothing until you pass ``--commit``.
* **All or nothing.** The whole file is applied in one transaction, so a bad
  row on line 400 does not leave you with 399 half-imported items.
* **Idempotent.** A row is matched to an existing item by barcode when the
  sheet has one, and otherwise by its natural key -- name plus location.
  Re-importing a corrected sheet updates rather than duplicating, which
  matters because the spreadsheets these come from rarely carry barcodes.

Export is the backup/interchange path and round-trips with import.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db
from .models import Item
from .service import (
    Actor,
    StockroomError,
    ValidationError,
    create_item,
    get_item_by_barcode,
    list_items,
    log_event,
    update_item,
)

# Columns understood on import. Only `name` is mandatory.
COLUMNS = (
    "name",
    "description",
    "quantity",
    "unit",
    "shelf",
    "sub_location",
    "barcode",
    "product_url",
    "min_quantity",
)

# Extra columns written on export (derived, ignored if fed back in).
EXPORT_COLUMNS = COLUMNS + ("available", "out_qty", "location", "status")

# Tolerated spellings for each column, so a sheet with "Product Link" or
# "Qty" imports without hand-editing the header row.
_ALIASES: dict[str, str] = {
    "item": "name", "item name": "name", "title": "name",
    "desc": "description", "notes": "description",
    "qty": "quantity", "count": "quantity", "total": "quantity",
    "storage unit": "unit", "cabinet": "unit", "storage": "unit",
    "shelf number": "shelf", "shelf no": "shelf",
    "bin": "sub_location", "drawer": "sub_location", "sublocation": "sub_location",
    "sub location": "sub_location", "detail": "sub_location",
    "upc": "barcode", "sku": "barcode", "code": "barcode",
    "url": "product_url", "link": "product_url", "product link": "product_url",
    "product url": "product_url",
    "min": "min_quantity", "minimum": "min_quantity", "reorder at": "min_quantity",
    "min quantity": "min_quantity",
}


def _normalize_header(name: str) -> str | None:
    """Map a spreadsheet header cell to a known column, or None to ignore."""
    key = (name or "").strip().lower().replace("_", " ")
    if key.replace(" ", "_") in COLUMNS:
        return key.replace(" ", "_")
    return _ALIASES.get(key)


@dataclass
class ImportResult:
    """What an import did, or (on a dry run) would do."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    committed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        mode = "Imported" if self.committed else "Dry run -- would import"
        lines = [
            f"{mode}: {len(self.created)} new, {len(self.updated)} updated, "
            f"{len(self.skipped)} unchanged, {len(self.errors)} error(s)."
        ]
        if self.ignored_columns:
            lines.append(
                "  Ignored unrecognised column(s): "
                + ", ".join(self.ignored_columns)
            )
        for label, rows in (("NEW", self.created), ("UPDATE", self.updated)):
            for row in rows:
                lines.append(f"  {label:6} {row}")
        for err in self.errors:
            lines.append(f"  ERROR  {err}")
        if not self.ok:
            lines.append(
                "\nNothing was written -- the whole file is applied or none of it is. "
                "Fix the error(s) above and re-run."
            )
        elif not self.committed:
            lines.append("\nNothing was written. Re-run with --commit to apply.")
        return "\n".join(lines)


def parse_rows(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Parse CSV text into normalized rows plus a list of ignored columns."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("The CSV file is empty.") from None

    mapping: dict[int, str] = {}
    ignored: list[str] = []
    for index, cell in enumerate(header):
        column = _normalize_header(cell)
        if column:
            mapping[index] = column
        elif cell.strip():
            ignored.append(cell.strip())

    if "name" not in mapping.values():
        raise ValidationError(
            "The CSV needs a 'name' column. Found: "
            + (", ".join(h.strip() for h in header if h.strip()) or "nothing")
        )

    rows: list[dict[str, str]] = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue  # blank separator line
        rows.append(
            {column: (raw[i].strip() if i < len(raw) else "")
             for i, column in mapping.items()}
        )
    return rows, ignored


def import_csv(
    conn: sqlite3.Connection,
    text: str,
    *,
    actor: Actor,
    commit: bool = False,
    source: str = "csv",
) -> ImportResult:
    """Import items from CSV text.

    With ``commit=False`` (the default) the work is done inside a transaction
    that is then rolled back, so the report reflects exactly what a real run
    would do -- including any conflicts -- without changing anything.
    """
    rows, ignored = parse_rows(text)
    result = ImportResult(committed=commit, ignored_columns=ignored)

    class _Rollback(Exception):
        """Internal signal to abandon a dry run's transaction."""

    def _apply() -> None:
        for line_no, row in enumerate(rows, start=2):
            name = row.get("name", "").strip()
            if not name:
                result.errors.append(f"line {line_no}: missing name")
                continue
            try:
                fields = _row_to_fields(row)
                existing = _find_existing(conn, row, fields)
                if existing is None:
                    create_item(conn, actor=actor, **fields)
                    result.created.append(f"line {line_no}: {name}")
                else:
                    before = existing.as_dict()
                    if all(before.get(k) == v for k, v in fields.items()):
                        result.skipped.append(f"line {line_no}: {name}")
                        continue
                    update_item(conn, actor=actor, item_id=existing.id, **fields)
                    result.updated.append(
                        f"line {line_no}: {name} [{existing.barcode}]"
                    )
            except StockroomError as exc:
                result.errors.append(f"line {line_no} ({name}): {exc}")

        if result.errors:
            # All-or-nothing: one bad row abandons the whole file.
            raise _Rollback
        if commit:
            log_event(
                conn,
                actor=actor,
                action="import.run",
                entity_type="system",
                summary=(
                    f"Imported {source}: {len(result.created)} created, "
                    f"{len(result.updated)} updated, {len(result.skipped)} unchanged"
                ),
                changes={"source": {"from": None, "to": source}},
            )
        else:
            raise _Rollback

    try:
        with db.transaction(conn):
            _apply()
    except _Rollback:
        pass
    if result.errors:
        result.committed = False
    return result


def _find_existing(
    conn: sqlite3.Connection, row: dict[str, str], fields: dict[str, Any]
):
    """Find the item this CSV row refers to, if it is already on record.

    Barcode is the reliable identity and wins when the sheet supplies one.
    Failing that, an item is identified by name plus its three location
    fields: two different "Arduino Uno R3" rows in different drawers are
    genuinely two stock entries, while the same name in the same place is the
    same entry being re-imported.
    """
    if row.get("barcode", "").strip():
        return get_item_by_barcode(conn, row["barcode"])

    match = conn.execute(
        """
        SELECT * FROM item_status
        WHERE name = ? COLLATE NOCASE
          AND unit = ? AND shelf = ?
          AND COALESCE(sub_location, '') = ?
          AND archived_at IS NULL
        """,
        (fields["name"], fields["unit"], fields["shelf"],
         fields["sub_location"] or ""),
    ).fetchone()
    return Item.from_row(match) if match else None


def _row_to_fields(row: dict[str, str]) -> dict[str, Any]:
    """Convert one CSV row into keyword arguments for create/update_item."""
    # _undefuse strips the apostrophe export_csv adds in front of a value that
    # would otherwise be read as a formula, so a file exported and re-imported
    # comes back with the name it went out with.
    fields: dict[str, Any] = {
        "name": _undefuse(row["name"].strip()),
        "description": _undefuse(row.get("description", "").strip()),
        "unit": _undefuse(row.get("unit", "").strip()),
        "shelf": _undefuse(row.get("shelf", "").strip()),
        "sub_location": _undefuse(row.get("sub_location", "").strip()) or None,
        "product_url": _undefuse(row.get("product_url", "").strip()) or None,
    }

    quantity = row.get("quantity", "").strip()
    if quantity:
        try:
            fields["quantity"] = int(float(quantity))
        except ValueError:
            raise ValidationError(f"quantity {quantity!r} is not a number") from None
    else:
        fields["quantity"] = 1

    minimum = row.get("min_quantity", "").strip()
    if minimum:
        try:
            fields["min_quantity"] = int(float(minimum))
        except ValueError:
            raise ValidationError(f"min_quantity {minimum!r} is not a number") from None
    else:
        fields["min_quantity"] = None

    if row.get("barcode", "").strip():
        fields["barcode"] = row["barcode"].strip()
    return fields


# Characters that make a spreadsheet treat a cell as a formula rather than as
# text. Excel and LibreOffice both do this, and a formula in a downloaded file
# runs against the machine of whoever opened it.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _defuse(value: Any) -> Any:
    """Stop a spreadsheet executing a cell that only looks like a formula.

    Item names and descriptions are free text, and not all of it is written by
    staff -- a requester's new-item request supplies the name that staff then
    create the item from. A name of `=HYPERLINK(...)` or a DDE payload is inert
    everywhere in this application and becomes live the moment somebody opens
    the export in Excel.

    Prefixing an apostrophe is the standard mitigation: spreadsheets read it as
    "this cell is text" and do not display it. :func:`_undefuse` takes it back
    off on import, so the round trip is unchanged -- which
    test_export_round_trips checks.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value
    return value


def _undefuse(value: str) -> str:
    """Remove one leading apostrophe added by :func:`_defuse`."""
    return value[1:] if value.startswith("'") else value


def export_csv(conn: sqlite3.Connection, *, include_archived: bool = False) -> str:
    """Serialize the inventory to CSV. Round-trips with :func:`import_csv`."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for item in list_items(conn, include_archived=include_archived):
        writer.writerow(
            {
                key: _defuse(value)
                for key, value in {
                    "name": item.name,
                    "description": item.description,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "shelf": item.shelf,
                    "sub_location": item.sub_location or "",
                    "barcode": item.barcode or "",
                    "product_url": item.product_url or "",
                    "min_quantity": (
                        "" if item.min_quantity is None else item.min_quantity
                    ),
                    "available": item.available,
                    "out_qty": item.out_qty,
                    "location": item.location,
                    "status": item.status_label,
                }.items()
            }
        )
    return buffer.getvalue()


def read_text(path: Path | str) -> str:
    """Read a CSV file, tolerating a UTF-8 BOM from Excel exports."""
    return Path(path).read_text(encoding="utf-8-sig")
