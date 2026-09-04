"""Item routes: browse, search, scan, create, edit, condition, archive, labels."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import barcodes, csvio, kits, search, service
from ..models import HOLD_STATE_LABELS
# Starlette's UploadFile, not FastAPI's. request.form() constructs the base
# class, and fastapi.UploadFile is a *subclass* of it -- so an isinstance check
# against the FastAPI one silently fails for every upload, and the route
# reports "no photo was chosen" for a photo that is right there in the body.
from starlette.datastructures import UploadFile

from ..photos import PhotoError
from ..service import ConflictError, NotFound, StockroomError
from .deps import get_conn, page, redirect, require_account, require_staff

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """The stockroom's home screen: scan box first, then what needs attention."""
    require_account(request)
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
    require_account(request)
    conn = get_conn()
    item = search.resolve_scan(conn, code)
    if item is not None:
        return redirect(f"/items/{item.id}")
    return redirect(f"/items?q={code}")


@router.get("/items", response_class=HTMLResponse)
def list_items(request: Request, q: str = "", unit: str = "", filter: str = ""):
    require_account(request)
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
        elif filter == "held":
            items = [i for i in items if i.held_qty > 0]
    else:
        items = service.list_items(
            conn,
            include_archived=(filter == "archived"),
            unit=unit or None,
            only_available=(filter == "available"),
            only_out=(filter == "out"),
            only_low_stock=(filter == "low"),
            only_held=(filter == "held"),
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
        storage_units=service.list_storage_units(conn),
    )


@router.get("/items/new", response_class=HTMLResponse)
def new_item_form(request: Request):
    require_staff(request)
    return page(request, "item_form.html", item=None,
                storage_units=service.list_storage_units(get_conn()))


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
    tracked: str = Form(""),
):
    actor = require_staff(request).as_actor()
    try:
        item = service.create_item(
            get_conn(), actor=actor, name=name, description=description,
            quantity=quantity, min_quantity=min_quantity or None, unit=unit,
            shelf=shelf, sub_location=sub_location, barcode=barcode,
            product_url=product_url, tracked=bool(tracked),
        )
    except StockroomError as exc:
        return redirect("/items/new", error=str(exc))
    return redirect(f"/items/{item.id}", ok=f"Added {item.name} ({item.barcode}).")


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int):
    """One item. Requesters see the shelf; staff see the paperwork.

    A requester can reach this page -- browsing the catalogue is how they
    decide what to ask for -- so the staff half is withheld *here* rather
    than only hidden in the template. The two are not the same thing: a
    template hides a control, but the route decides whether the data was
    ever rendered into the page at all. Every field below the split names a
    person -- the borrower datalist is every email address the stockroom
    holds, the loan tables are who has what right now -- and "who had it"
    is the stockroom's business, not the next requester's. This mirrors
    routes_requests.request_detail, which withholds `overlaps` for the
    same reason.
    """
    account = require_account(request)
    conn = get_conn()
    from .. import db

    item = service.get_item(conn, item_id)
    context = dict(
        item=item,
        photos=service.list_photos(conn, item_id),
        barcode_svg=barcodes.render_svg(item.barcode) if item.barcode else "",
        now=db.utcnow(),
        next_url=f"/items/{item_id}",
    )

    if account.is_staff:
        loans = service.list_loans(conn, item_id=item_id)
        units = service.list_units(conn, item_id=item_id) if item.is_tracked else []
        context.update(
            open_loans=[l for l in loans if l.is_open],
            past_loans=[l for l in loans if not l.is_open][:40],
            people=service.list_people(conn),
            kits_using=kits.kits_containing(conn, item_id),
            holds=service.list_holds(conn, item_id=item_id, open_only=True),
            past_holds=service.list_holds(conn, item_id=item_id, open_only=False)[:20],
            units=units,
            # Only units that can actually go out of the door: sound, still
            # owned, and not already in somebody's bag. is_available covers the
            # first two -- it is the question the condition machinery asks --
            # and would happily offer a camera that is already lent out.
            lendable_units=[u for u in units if u.is_lendable],
            hold_states=HOLD_STATE_LABELS,
        )

    return page(request, "item_detail.html", **context)


@router.get("/items/{item_id}/edit", response_class=HTMLResponse)
def edit_item_form(request: Request, item_id: int):
    require_staff(request)
    conn = get_conn()
    return page(
        request,
        "item_form.html",
        item=service.get_item(conn, item_id),
        storage_units=service.list_storage_units(conn),
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
    tracked: str = Form(""),
):
    actor = require_staff(request).as_actor()
    try:
        service.update_item(
            get_conn(), actor=actor, item_id=item_id, name=name,
            description=description, quantity=quantity, min_quantity=min_quantity,
            unit=unit, shelf=shelf, sub_location=sub_location, barcode=barcode,
            product_url=product_url, tracked=1 if tracked else 0,
        )
    except StockroomError as exc:
        return redirect(f"/items/{item_id}/edit", error=str(exc))
    return redirect(f"/items/{item_id}", ok="Changes saved.")


@router.post("/items/{item_id}/barcode")
def assign_barcode(request: Request, item_id: int):
    actor = require_staff(request).as_actor()
    try:
        item = service.assign_barcode(get_conn(), actor=actor, item_id=item_id)
    except StockroomError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"Assigned barcode {item.barcode}.")


@router.post("/items/{item_id}/archive")
def archive_item(request: Request, item_id: int, reason: str = Form("")):
    actor = require_staff(request).as_actor()
    try:
        item = service.archive_item(get_conn(), actor=actor, item_id=item_id, reason=reason)
    except ConflictError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"{item.name} archived.")


@router.post("/items/{item_id}/restore")
def restore_item(request: Request, item_id: int):
    actor = require_staff(request).as_actor()
    try:
        item = service.restore_item(get_conn(), actor=actor, item_id=item_id)
    except ConflictError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"{item.name} restored.")


# ---------------------------------------------------------------------------
# condition: units out of service, and individually tracked units
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/holds")
def open_hold(request: Request, item_id: int, state: str = Form(...),
              quantity: int = Form(1), unit_id: str = Form(""),
              note: str = Form("")):
    """Take units out of service."""
    actor = require_staff(request).as_actor()
    try:
        service.open_hold(
            get_conn(), actor=actor, item_id=item_id, state=state,
            quantity=quantity,
            unit_id=int(unit_id) if unit_id.strip() else None,
            note=note,
        )
    except (StockroomError, ValueError) as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok="Recorded. It is no longer lendable.")


@router.post("/holds/{hold_id}/state")
def change_hold(request: Request, hold_id: int, state: str = Form(...),
                note: str = Form("")):
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        hold = service.change_hold(conn, actor=actor, hold_id=hold_id,
                                   state=state, note=note)
    except StockroomError as exc:
        return redirect("/items", error=str(exc))
    return redirect(f"/items/{hold.item_id}",
                    ok=f"Now recorded as {hold.state_label.lower()}.")


@router.post("/holds/{hold_id}/close")
def close_hold(request: Request, hold_id: int, resolution: str = Form("")):
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        hold = service.close_hold(conn, actor=actor, hold_id=hold_id,
                                  resolution=resolution)
    except StockroomError as exc:
        return redirect("/items", error=str(exc))
    return redirect(f"/items/{hold.item_id}", ok="Back in service.")


@router.post("/items/{item_id}/units")
def create_unit(request: Request, item_id: int, asset_tag: str = Form(""),
                serial: str = Form(""), note: str = Form("")):
    """Register one individual unit of a tracked item."""
    actor = require_staff(request).as_actor()
    try:
        unit = service.create_unit(get_conn(), actor=actor, item_id=item_id,
                                   asset_tag=asset_tag, serial=serial, note=note)
    except StockroomError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok=f"Registered {unit.label}.")


@router.post("/units/{unit_id}/retire")
def retire_unit(request: Request, unit_id: int, reason: str = Form("")):
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        unit = service.retire_unit(conn, actor=actor, unit_id=unit_id,
                                   reason=reason)
    except StockroomError as exc:
        return redirect("/items", error=str(exc))
    return redirect(f"/items/{unit.item_id}", ok=f"Retired {unit.label}.")


# ---------------------------------------------------------------------------
# photos
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/photos")
async def upload_photo(request: Request, item_id: int):
    """Attach a photo to an item.

    Reads the form by hand rather than through FastAPI's `UploadFile` in the
    signature, because the CSRF middleware has already consumed and cached the
    body -- `request.form()` here replays that cached body, which is exactly
    what test_csrf_middleware_does_not_eat_the_request_body exists to protect.
    """
    actor = require_staff(request).as_actor()
    form = await request.form()
    upload = form.get("photo")
    caption = str(form.get("caption", ""))

    if not isinstance(upload, UploadFile) or not upload.filename:
        return redirect(f"/items/{item_id}", error="No photo was chosen.")

    data = await upload.read()
    try:
        service.add_photo(get_conn(), actor=actor, item_id=item_id, data=data,
                          caption=caption)
    except (StockroomError, PhotoError) as exc:
        return redirect(f"/items/{item_id}", error=str(exc))
    return redirect(f"/items/{item_id}", ok="Photo added.")


@router.post("/photos/{photo_id}/primary")
def set_primary_photo(request: Request, photo_id: int):
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        photo = service.set_primary_photo(conn, actor=actor, photo_id=photo_id)
    except StockroomError as exc:
        return redirect("/items", error=str(exc))
    return redirect(f"/items/{photo.item_id}", ok="That is now the main photo.")


@router.post("/photos/{photo_id}/remove")
def remove_photo(request: Request, photo_id: int):
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        photo = service.remove_photo(conn, actor=actor, photo_id=photo_id)
    except StockroomError as exc:
        return redirect("/items", error=str(exc))
    return redirect(f"/items/{photo.item_id}", ok="Photo removed.")


@router.get("/labels", response_class=HTMLResponse)
def labels(request: Request, ids: str = "", unit: str = ""):
    """A printable label sheet. Staff only -- it is a bulk inventory dump.

    ``?ids=1,2,3`` prints specific items; with no ids it prints every item
    that has a barcode (optionally narrowed to one storage unit), which is
    the "we just set the room up, label everything" case.
    """
    require_staff(request)
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
    # Through page(), not TemplateResponse directly. labels.html does not
    # extend base.html, but it still has one inline <script> -- the Print
    # button -- and that script needs the request's CSP nonce. Rendering the
    # template straight left csp_nonce undefined, so the page shipped
    # `<script nonce="">`, which the CSP header on the very same response
    # refuses. The button did nothing, silently, which is the whole reason
    # this codebase bans inline handlers.
    return page(request, "labels.html", items=rows)


@router.get("/export.csv")
def export_csv(request: Request, include_archived: bool = False):
    """Download the whole inventory as CSV -- backup and interchange.

    Staff only. The public page already answers "what is available"; this is
    the complete record, including locations and archived stock.
    """
    require_staff(request)
    body = csvio.export_csv(get_conn(), include_archived=include_archived)
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stockroom-inventory.csv"'},
    )
