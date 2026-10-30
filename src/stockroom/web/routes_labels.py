"""The USB label printer: choose a section, then print one label at a time.

One at a time is the point. The QL-500 has no cutter, and a job of several
labels comes out as one unbroken strip, so each label is its own job and the
page waits for "Print next" -- which someone presses after tearing the last
one off. "Print the rest" sends them all without stopping, for die-cut labels
that peel off a backing and need no cutting.

The queue of labels still to print travels in the form and the URL, like the
counter basket: nothing is stored, so closing the tab half-way leaves nothing
behind. A queue entry is ``i<item id>`` or ``u<unit id>``.

The office-printer sheet stays at /labels in routes_items.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from .. import labelprinter, service
from ..service import NotFound
from .deps import get_conn, page, redirect, require_staff, safe_path

router = APIRouter()


def _parse_queue(raw: list[str]) -> list[str]:
    """Clean queue tokens out of form fields, each of which may be a list."""
    tokens = []
    for field in raw:
        for token in field.split(","):
            token = token.strip()
            if len(token) > 1 and token[0] in "iu" and token[1:].isdigit():
                tokens.append(token)
    return tokens


def _label_for(conn, token: str) -> labelprinter.Label | None:
    """The label a queue token stands for, or None if it has no code to print."""
    try:
        if token[0] == "i":
            item = service.get_item(conn, int(token[1:]))
            return labelprinter.for_item(item) if item.barcode else None
        unit = service.get_unit(conn, int(token[1:]))
        if not unit.asset_tag or unit.is_retired:
            return None
        return labelprinter.for_unit(unit, service.get_item(conn, unit.item_id))
    except NotFound:
        return None


def _section_rows(conn, *, unit: str, shelf: str, with_units: bool):
    """Everything in one part of the room that has a code to print, in shelf order.

    Shelf order rather than name order because the strip is applied walking
    along the shelf, and a strip in alphabetical order has you crossing the
    room for every label.
    """
    items = [
        i for i in service.list_items(conn, unit=unit or None)
        if not shelf or i.shelf == shelf
    ]
    items.sort(key=lambda i: ((i.unit or "").lower(), (i.shelf or "").lower(),
                              (i.sub_location or "").lower(), i.name.lower()))
    rows = []
    for item in items:
        if item.barcode:
            rows.append({"token": f"i{item.id}", "item": item, "unit": None,
                         "code": item.barcode})
        if with_units and item.is_tracked:
            for u in service.list_units(conn, item_id=item.id):
                if u.asset_tag:
                    rows.append({"token": f"u{u.id}", "item": item, "unit": u,
                                 "code": u.asset_tag})
    return rows


@router.get("/labels/choose", response_class=HTMLResponse)
def choose(request: Request, unit: str = "", shelf: str = "",
           units: list[str] = Query([])):
    """Pick a storage unit and shelf, tick what needs a label."""
    require_staff(request)
    conn = get_conn()
    # Units' asset tags are included unless asked not to be: off by default,
    # a whole class of labels went unprinted without anyone noticing they
    # were missing, which is worse than unticking a row that already has a
    # sticker. The form sends a hidden "0" before the checkbox's "1", because
    # an unticked box sends nothing and nothing has to mean "the default".
    with_units = "1" in units if units else True
    in_unit = service.list_items(conn, unit=unit) if unit else []
    shelves = sorted({i.shelf for i in in_unit if i.shelf}, key=str.lower)
    if shelf not in shelves:
        shelf = ""
    rows = _section_rows(conn, unit=unit, shelf=shelf, with_units=with_units)
    item_ids = [str(r["item"].id) for r in rows if r["unit"] is None]
    return page(
        request,
        "labels_choose.html",
        rows=rows,
        unit=unit,
        shelf=shelf,
        with_units=with_units,
        storage_units=service.list_storage_units(conn),
        shelves=shelves,
        printer=labelprinter.connected(),
        sheet_url="/labels?ids=" + ",".join(item_ids) if item_ids else "",
        next_url="/labels/choose?" + urlencode(
            {k: v for k, v in (("unit", unit), ("shelf", shelf),
                               ("units", "1" if with_units else "0")) if v}
        ),
    )


@router.get("/labels/run", response_class=HTMLResponse)
def run(request: Request, queue: str = "", done: int = 0, total: int = 0,
        next: str = ""):
    """Between two labels: tear that one off, then print the next."""
    require_staff(request)
    conn = get_conn()
    tokens = _parse_queue([queue])
    back = safe_path(next, "/labels/choose")
    if not tokens:
        return redirect(back)
    upcoming = _label_for(conn, tokens[0])
    return page(
        request,
        "labels_run.html",
        queue=",".join(tokens),
        remaining=len(tokens),
        done=done,
        total=max(total, done + len(tokens)),
        upcoming=upcoming,
        back=back,
        printer=labelprinter.connected(),
    )


@router.post("/labels/print")
def print_next(request: Request, pick: list[str] = Form([]), queue: str = Form(""),
               done: int = Form(0), total: int = Form(0), next: str = Form("")):
    """Print the first label in the queue, then come back for the next.

    The first press of a batch comes from the chooser's ticked boxes
    (``pick``); every later one from the run page's ``queue``.
    """
    account = require_staff(request)
    conn = get_conn()
    back = safe_path(next, "/labels/choose")
    tokens = _parse_queue(pick + [queue])
    total = max(total, done + len(tokens))

    # Skip anything that has lost its code since the page was drawn -- an
    # item archived, a unit retired -- rather than stopping the batch on it.
    label = token = None
    while tokens and label is None:
        token = tokens.pop(0)
        label = _label_for(conn, token)
    if label is None:
        return redirect(back, error="Nothing to print: none of those have a barcode.")

    try:
        labelprinter.print_labels([label], by=account.email)
    except labelprinter.LabelPrinterError as exc:
        # Back to the run page with this label still first in line, so the
        # same button prints it again once the cover is shut or the tape is in.
        query = urlencode({"queue": ",".join([token] + tokens), "done": done,
                           "total": total, "next": back})
        return redirect(f"/labels/run?{query}", error=str(exc))

    done += 1
    if not tokens:
        return redirect(back, ok=f"Printed {done} label(s).")
    query = urlencode({"queue": ",".join(tokens), "done": done, "total": total,
                       "next": back})
    return redirect(f"/labels/run?{query}", ok=f"Printed {label.code}.")


@router.post("/labels/print-rest")
def print_rest(request: Request, queue: str = Form(""), next: str = Form("")):
    """Everything left in the queue as one strip, without stopping."""
    account = require_staff(request)
    conn = get_conn()
    back = safe_path(next, "/labels/choose")
    labels = [l for l in (_label_for(conn, t) for t in _parse_queue([queue])) if l]
    if not labels:
        return redirect(back, error="Nothing left to print.")
    try:
        labelprinter.submit(labels, by=account.email)
    except labelprinter.LabelPrinterError as exc:
        query = urlencode({"queue": queue, "next": back})
        return redirect(f"/labels/run?{query}", error=str(exc))
    return redirect(back, ok=f"Printing {len(labels)} label(s) without stopping.")
