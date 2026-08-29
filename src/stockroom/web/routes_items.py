"""Item routes: browse, search, scan, create, edit, archive, labels."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import barcodes, csvio, search, service
from ..service import ConflictError, NotFound, StockroomError
from .deps import get_conn, page, redirect, require_actor

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """The stockroom's home screen: scan box first, then what needs attention."""
    conn = get_conn()
    from .. import db

    return page(
        request,
        "dashboard.html",
        now=db.utcnow(),
        overdue=service.list_loans(conn, overdue_only=True),
        low_stock=service.list_items(conn, only_low_stock=True),
        open_loans=service.list_loans(conn, open_only=True, limit=15),
        recent=service.list_events(conn, limit=12),
        next_url="/",
    )


@router.get("/scan", response_class=HTMLResponse)
def scan(request: Request, code: str = ""):
    """Resolve a scanned barcode to an item, or fall back to the search list.

    A USB barcode scanner types the code and presses Enter, so this endpoint
    is what the dashboard's search box submits to. An exact, unambiguous
    match jumps straight to the item -- that is the whole point of scanning.
    """
    conn = get_conn()
    item = search.resolve_scan(conn, code)
    if item is not None:
        return redirect(f"/items/{item.id}")
    return redirect(f"/items?q={code}")


@router.get("/items", response_class=HTMLResponse)
def list_items(request: Request, q: str = "", unit: str = "", filter: str = ""):
    conn = get_conn()
    if q.strip():
        items = search.search_items(conn, q, include_archived=(filter == "archived"))
        if unit:
            items = [i for i in items if i.unit == unit]
        if filter == "available":
            items = [i for i in items if i.available > 0]
        elif filter == "out":
            items = [i for i in items if i.out_qty > 0]
        elif filter == "low":
            items = [i for i in items if i.is_low_stock]
    else:
        items = service.list_items(
            conn,
            include_archived=(filter == "archived"),
            unit=unit or None,
            only_available=(filter == "available"),
            only_out=(filter == "out"),
            only_low_stock=(filter == "low"),
        )
        if filter == "archived":
            items = [i for i in items if i.is_archived]

    return page(
        request,
        "items.html",
        items=items,
        query=q,
        unit=unit,
        filter=filter,
        units=service.list_units(conn),
    )


@router.get("/items/new", response_class=HTMLResponse)
def new_item_form(request: Request):
    require_actor(request)
    return page(request, "item_form.html", item=None, units=service.list_units(get_conn()))


@router.post("/items/new")
def create_item(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    quantity: int = Form(1),
    min_quantity: str = Form(""),
    unit: str = Form(""),
    shelf: str = Form(""),
    sub_location: str = Form(""),
    barcode: str = Form(""),
    product_url: str = Form(""),
):
    actor = require_actor(request)
    try:
        item = service.create_item(
            get_conn(), actor=actor, name=name, description=description,
            quantity=quantity, min_quantity=min_quantity or None, unit=unit,
            shelf=shelf, sub_location=sub_location, barcode=barcode,
            product_url=product_url,
        )
    except StockroomError as exc:
        return redirect("/items/new", error=str(exc))
    return redirect(f"/items/{item.id}", ok=f"Added {item.name} ({item.barcode}).")


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int):
    conn = get_conn()
    from .. import db

    item = service.get_item(conn, item_id)
    loans = service.list_loans(conn, item_id=item_id)
    return page(
        request,
        "item_detail.html",
        item=item,
        open_loans=[l for l in loans if l.is_open],
        past_loans=[l for l in loans if not l.is_open][:40],
        people=service.list_people(conn),
        barcode_svg=barcodes.render_svg(item.barcode) if item.barcode else "",
        now=db.utcnow(),
        next_url=f"/items/{item_id}",
    )


@router.get("/items/{item_id}/edit", response_class=HTMLResponse)
def edit_item_form(request: Request, item_id: int):
    require_actor(request)
    conn = get_conn()
    return page(
        request,
        "item_form.html",
        item=service.get_item(conn, item_id),
        units=service.list_units(conn),
    )


@router.post("/items/{item_id}/edit")
def edit_item(
    request: Request,
    item_id: int,
    name: str = Form(...),
    description: str = Form(""),
    quantity: int = Form(...),
    min_quantity: str = Form(""),
    unit: str = Form(""),
    shelf: str = Form(""),
    sub_location: str = Form(""),
    barcode: str = Form(""),
    product_url: str = Form(""),
):
    actor = require_actor(request)
    try:
        service.update_item(
            get_conn(), actor=actor, item_id=item_id, name=name,
            description=description, quantity=quantity, min_quantity=min_quantity,
            unit=unit, shelf=shelf, sub_location=sub_location, barcode=barcode,
            product_url=product_url,
        )
    except StockroomError as exc:
        return redirect(f"/items/{item_id}/edit", error=str(exc))
    return redirect(f"/items/{item_id}", ok="Changes saved.")


@router.post("/items/{item_id}/barcode")
def assign_barcode(request: Request, item_id: int):
    actor = require_actor(request)
    try:
        item = service.assign_barcode(get_conn(), actor=actor, item_id=item_id)
    except StockroomError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"Assigned barcode {item.barcode}.")


@router.post("/items/{item_id}/archive")
def archive_item(request: Request, item_id: int, reason: str = Form("")):
    actor = require_actor(request)
    try:
        item = service.archive_item(get_conn(), actor=actor, item_id=item_id, reason=reason)
    except ConflictError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"{item.name} archived.")


@router.post("/items/{item_id}/restore")
def restore_item(request: Request, item_id: int):
    actor = require_actor(request)
    try:
        item = service.restore_item(get_conn(), actor=actor, item_id=item_id)
    except ConflictError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"{item.name} restored.")


@router.get("/labels", response_class=HTMLResponse)
def labels(request: Request, ids: str = "", unit: str = ""):
    """A printable label sheet.

    ``?ids=1,2,3`` prints specific items; with no ids it prints every item
    that has a barcode (optionally narrowed to one storage unit), which is
    the "we just set the room up, label everything" case.
    """
    conn = get_conn()
    if ids.strip():
        wanted = [int(part) for part in ids.replace(" ", "").split(",") if part.isdigit()]
        items = []
        for item_id in wanted:
            try:
                items.append(service.get_item(conn, item_id))
            except NotFound:
                continue
    else:
        items = service.list_items(conn, unit=unit or None)

    rows = [
        {"item": item, "svg": barcodes.render_svg(item.barcode)}
        for item in items
        if item.barcode
    ]
    from .deps import templates

    return templates.TemplateResponse(request, "labels.html", {"items": rows})


@router.get("/export.csv")
def export_csv(include_archived: bool = False):
    """Download the whole inventory as CSV -- backup and interchange."""
    body = csvio.export_csv(get_conn(), include_archived=include_archived)
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stockroom-inventory.csv"'},
    )
