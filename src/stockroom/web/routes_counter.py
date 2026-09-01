"""The counter: scan, scan, scan, hand it over.

The item page is the right shape for one careful checkout and the wrong shape
for a queue of students. Five items to one person means five page loads, five
borrower lookups and five chances to be interrupted half way.

    HOW THE BASKET WORKS, AND WHY THERE IS NO JAVASCRIPT

    The basket lives in hidden form fields. Every scan POSTs the whole form
    back; the server appends a line and re-renders with the basket restored as
    hidden inputs, focus back on the scan box. The browser holds the state
    because the form does.

    The alternatives were worse. Client-side state needs JavaScript, and the
    CSP has no 'unsafe-inline' -- an inline handler fails silently. A
    server-side draft table needs a mutation, a lifetime and a cleanup job for
    something that is not a domain fact. A session-scoped basket breaks the
    moment two staff share the counter machine.

    Accumulating a line mutates nothing, so re-submitting one is harmless.
    Only "Check out" writes, and it writes the whole basket in one
    transaction via service.checkout_many.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import db, kits, search, service
from ..service import NotFound, StockroomError
from .deps import get_conn, local_to_utc, page, redirect, require_staff

router = APIRouter()


# A basket line is (item_id, quantity, unit_id). unit_id is None for the
# countable majority; when it is set the line is one named object, scanned by
# its asset tag, and its quantity is always 1.
Line = tuple[int, int, "int | None"]


def _parse_basket(
    item_ids: list[str], quantities: list[str], unit_ids: list[str] | None = None
) -> list[Line]:
    """Rebuild the basket from the hidden fields the browser sent back.

    Deliberately forgiving: a malformed line is dropped rather than raising.
    These values came from our own form, so a bad one means a bug or a fiddled
    request, and neither is worth showing a stack trace to somebody with a
    queue in front of them.
    """
    units = list(unit_ids or [])
    # zip() would truncate every line if the unit column were short, so pad
    # rather than relying on all three lists arriving the same length.
    units += [""] * (len(item_ids) - len(units))

    basket: list[Line] = []
    for raw_id, raw_qty, raw_unit in zip(item_ids, quantities, units):
        try:
            item_id, quantity = int(raw_id), int(raw_qty)
        except (TypeError, ValueError):
            continue
        try:
            unit_id = int(raw_unit) if str(raw_unit).strip() else None
        except (TypeError, ValueError):
            unit_id = None
        if quantity > 0:
            basket.append((item_id, quantity, unit_id))
    return basket


def _merge(
    basket: list[Line], item_id: int, quantity: int, unit_id: int | None = None
) -> list[Line]:
    """Add to the basket, combining with an existing line for the same item.

    Scanning the same box twice means two of them, not two lines saying one.

    A named unit is the exception: two camera bodies are two distinct objects,
    so they get a line each and scanning the same tag twice is a no-op rather
    than "two of that exact camera", which is not a thing that can exist.
    """
    if unit_id is not None:
        if any(line[2] == unit_id for line in basket):
            return basket
        return basket + [(item_id, 1, unit_id)]

    merged: list[Line] = []
    added = False
    for line in basket:
        if line[2] is None and line[0] == item_id and not added:
            merged.append((item_id, line[1] + quantity, None))
            added = True
        else:
            merged.append(line)
    if not added:
        merged.append((item_id, quantity, None))
    return merged


def _line_key(line: Line) -> str:
    """How a basket line is named in the remove button.

    Item id alone was enough while a basket held one line per item; with units
    it is not, because four bodies of the same item are four lines.
    """
    item_id, _, unit_id = line
    return f"{item_id}" if unit_id is None else f"{item_id}:{unit_id}"


def _render(request: Request, basket, *, borrower="", borrower_name="",
            due_at="", note="", error="", ok=""):
    conn = get_conn()
    lines = []
    for item_id, quantity, unit_id in basket:
        try:
            item = service.get_item(conn, item_id)
        except NotFound:
            continue
        unit = None
        if unit_id is not None:
            try:
                unit = service.get_unit(conn, unit_id)
            except NotFound:
                continue
        lines.append({"item": item, "quantity": quantity, "unit": unit,
                      "unit_id": unit_id,
                      "key": _line_key((item_id, quantity, unit_id)),
                      # A named unit that somebody else took between the scan
                      # and the checkout is short too, and the availability
                      # count alone would not notice.
                      "short": quantity > item.available
                      or (unit is not None and unit.is_on_loan)})
    context = {
        "lines": lines,
        "basket_total": sum(l["quantity"] for l in lines),
        "blocked": [l for l in lines if l["short"]],
        "people": service.list_people(conn),
        "kits": [k for k in kits.list_kits(conn) if k.lines],
        "borrower": borrower,
        "borrower_name": borrower_name,
        "due_at": due_at,
        "note": note,
    }
    # Only set these when there is something to say. deps.page() falls back to
    # the ?ok= / ?error= query parameters via setdefault, and passing an empty
    # string here would win that setdefault and silently swallow the flash
    # message from a redirect.
    if error:
        context["error"] = error
    if ok:
        context["ok"] = ok
    return page(request, "counter.html", **context)


@router.get("/counter", response_class=HTMLResponse)
def counter(request: Request):
    require_staff(request)
    return _render(request, [])


@router.post("/counter/add")
def add_to_basket(
    request: Request,
    code: str = Form(""),
    kit_id: str = Form(""),
    item_id: list[str] = Form([]),
    quantity: list[str] = Form([]),
    unit_id: list[str] = Form([]),
    borrower: str = Form(""),
    borrower_name: str = Form(""),
    due_at: str = Form(""),
    note: str = Form(""),
):
    """Add a scanned item, or a whole kit, to the basket. Writes nothing."""
    require_staff(request)
    conn = get_conn()
    basket = _parse_basket(item_id, quantity, unit_id)
    error = ""

    if kit_id.strip():
        try:
            kit = kits.get_kit(conn, int(kit_id))
        except (NotFound, ValueError):
            error = "That kit no longer exists."
        else:
            for line_item, line_qty in kits.basket_lines(kit):
                basket = _merge(basket, line_item, line_qty)
    elif code.strip():
        # resolve(), not resolve_scan(): an asset tag names one camera body,
        # and the whole point of tracking units is that the basket records
        # which one rather than "a Canon".
        found = search.resolve(conn, code)
        if found is None:
            error = (
                f"Nothing matched “{code.strip()}”. Scan again, or search for "
                "it on the Items page."
            )
        elif found.item.is_archived:
            error = f"{found.item.name} is archived."
        elif found.unit is not None and not found.unit.is_lendable:
            unit = found.unit
            error = (
                f"{unit.label} is already checked out"
                + (f" to {unit.borrower_name}." if unit.borrower_name else ".")
                if unit.is_on_loan
                else f"{unit.label} is recorded as {unit.state_label.lower()}."
            )
        else:
            basket = _merge(
                basket, found.item.id, 1,
                found.unit.id if found.unit else None,
            )

    return _render(request, basket, borrower=borrower, borrower_name=borrower_name,
                   due_at=due_at, note=note, error=error)


@router.post("/counter/remove")
def remove_from_basket(
    request: Request,
    drop: str = Form(...),
    item_id: list[str] = Form([]),
    quantity: list[str] = Form([]),
    unit_id: list[str] = Form([]),
    borrower: str = Form(""),
    borrower_name: str = Form(""),
    due_at: str = Form(""),
    note: str = Form(""),
):
    require_staff(request)
    # `drop` identifies a line, and with units there can be several lines for
    # one item -- four camera bodies to one person is four lines. A unit line
    # is dropped by its own key so removing one does not take its siblings.
    basket = [
        line for line in _parse_basket(item_id, quantity, unit_id)
        if _line_key(line) != drop
    ]
    return _render(request, basket, borrower=borrower, borrower_name=borrower_name,
                   due_at=due_at, note=note)


@router.post("/counter/checkout")
def commit_basket(
    request: Request,
    item_id: list[str] = Form([]),
    quantity: list[str] = Form([]),
    unit_id: list[str] = Form([]),
    borrower: str = Form(""),
    borrower_name: str = Form(""),
    due_at: str = Form(""),
    note: str = Form(""),
):
    """Check the whole basket out to one person, in one transaction."""
    actor = require_staff(request).as_actor()
    conn = get_conn()
    basket = _parse_basket(item_id, quantity, unit_id)

    if not basket:
        return _render(request, basket, borrower=borrower,
                       borrower_name=borrower_name, due_at=due_at, note=note,
                       error="The basket is empty.")
    if not borrower.strip():
        return _render(request, basket, borrower=borrower,
                       borrower_name=borrower_name, due_at=due_at, note=note,
                       error="Who is taking these?")

    existing = service.find_person_by_email(conn, borrower)
    name = borrower_name.strip() or (
        existing.name if existing else borrower.split("@")[0]
    )
    try:
        loans = service.checkout_many(
            conn, actor=actor, lines=basket,
            person_id=existing.id if existing else None,
            person_name=name, person_email=borrower,
            due_at=local_to_utc(due_at, end=True),
            note=note,
        )
    except StockroomError as exc:
        # Nothing was written -- checkout_many is one transaction -- so the
        # basket is still exactly as the operator left it.
        return _render(request, basket, borrower=borrower,
                       borrower_name=borrower_name, due_at=due_at, note=note,
                       error=str(exc))

    total = sum(loan.quantity for loan in loans)
    return redirect(
        "/counter",
        ok=f"{total} unit(s) across {len(loans)} item(s) checked out to {name}.",
    )


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------


@router.get("/counter/return", response_class=HTMLResponse)
def return_desk(request: Request, person_id: str = ""):
    """Everything one person currently has, ready to hand back in one go."""
    require_staff(request)
    conn = get_conn()
    person = None
    loans = []
    if person_id.strip():
        try:
            person = service.get_person(conn, int(person_id))
            loans = service.list_loans(conn, person_id=person.id, open_only=True)
        except (NotFound, ValueError):
            person = None
    return page(
        request,
        "counter_return.html",
        person=person,
        loans=loans,
        people=service.list_people(conn),
        now=db.utcnow(),
    )


@router.post("/counter/return")
def return_basket(
    request: Request,
    person_id: int = Form(...),
    loan_id: list[str] = Form([]),
    note: str = Form(""),
):
    """Close every ticked loan in one transaction."""
    actor = require_staff(request).as_actor()
    ids = []
    for raw in loan_id:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    if not ids:
        return redirect(f"/counter/return?person_id={person_id}",
                        error="Nothing was ticked.")
    try:
        closed = service.return_many(get_conn(), actor=actor, loan_ids=ids, note=note)
    except StockroomError as exc:
        return redirect(f"/counter/return?person_id={person_id}", error=str(exc))
    return redirect(
        f"/counter/return?person_id={person_id}",
        ok=f"Returned {len(closed)} item(s).",
    )
