"""The three request forms, the staff inbox, and open hours."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import config, db, requests_service as rq, service
from ..service import StockroomError
from .deps import (
    Forbidden,
    get_conn,
    page,
    redirect,
    require_account,
    require_staff,
    safe_path,
)

router = APIRouter()

# Abuse control, not a security boundary: stops one person filing a hundred
# requests by holding down a key. Deliberately generous.
from ..security import RateLimiter

_submit_limit = RateLimiter(limit=20, per_seconds=3600)


@router.get("/requests", response_class=HTMLResponse)
def inbox(request: Request, status: str = "", kind: str = ""):
    """Staff inbox: everything awaiting a decision."""
    require_staff(request)
    conn = get_conn()
    from .. import accounts as accounts_module

    found = rq.list_requests(conn, status=status or None, kind=kind or None)
    # Oldest pending first. The default ordering puts pending at the top but
    # newest within it, which buries exactly the request that has been waiting
    # longest -- and with no email, waiting unseen is the only way the request
    # workflow actually fails.
    found.sort(key=lambda r: (r.status != "pending", r.created_at))

    return page(
        request,
        "requests.html",
        requests=found,
        status_filter=status,
        kind_filter=kind,
        kinds=rq.KIND_LABELS,
        stale_count=rq.count_stale(conn),
        stale_days=config.REQUEST_STALE_DAYS,
        pending_account_list=accounts_module.list_accounts(conn, status="pending"),
        open_hours=rq.list_open_hours(conn),
        now=db.utcnow(),
    )


@router.get("/requests/mine", response_class=HTMLResponse)
def my_requests(request: Request):
    account = require_account(request)
    return page(
        request,
        "my_requests.html",
        requests=rq.list_requests(get_conn(), requester_id=account.id),
        kinds=rq.KIND_LABELS,
    )


@router.get("/requests/new/{kind}", response_class=HTMLResponse)
def new_request_form(request: Request, kind: str):
    require_account(request)
    if kind not in rq.KINDS:
        return redirect("/requests/mine", error="Unknown request type.")
    conn = get_conn()
    return page(
        request,
        "request_form.html",
        kind=kind,
        kind_label=rq.KIND_LABELS[kind],
        items=service.list_items(conn) if kind == "borrow" else [],
    )


@router.post("/requests/new/borrow")
def submit_borrow(
    request: Request,
    item_id: int = Form(...),
    quantity: int = Form(1),
    needed_from: str = Form(""),
    needed_until: str = Form(""),
    note: str = Form(""),
):
    account = require_account(request)
    _check_limit(account.id)
    try:
        created = rq.submit_borrow(
            get_conn(), actor=account.as_actor(), requester_id=account.id,
            item_id=item_id, quantity=quantity,
            needed_from=_as_utc(needed_from), needed_until=_as_utc(needed_until, end=True),
            note=note,
        )
    except StockroomError as exc:
        return redirect("/requests/new/borrow", error=str(exc))
    return redirect(
        "/requests/mine",
        ok=f"Request #{created.id} submitted. Staff will review it.",
    )


@router.post("/requests/new/new_item")
def submit_new_item(
    request: Request,
    proposed_name: str = Form(...),
    proposed_description: str = Form(""),
    proposed_url: str = Form(""),
    proposed_quantity: int = Form(1),
    proposed_vendor: str = Form(""),
    note: str = Form(""),
):
    account = require_account(request)
    _check_limit(account.id)
    try:
        created = rq.submit_new_item(
            get_conn(), actor=account.as_actor(), requester_id=account.id,
            name=proposed_name, description=proposed_description,
            url=proposed_url, quantity=proposed_quantity,
            vendor=proposed_vendor, note=note,
        )
    except StockroomError as exc:
        return redirect("/requests/new/new_item", error=str(exc))
    return redirect("/requests/mine", ok=f"Request #{created.id} submitted.")


@router.post("/requests/new/open_hours")
def submit_open_hours(
    request: Request,
    window_start: str = Form(...),
    window_end: str = Form(...),
    purpose: str = Form("both"),
    note: str = Form(""),
):
    account = require_account(request)
    _check_limit(account.id)
    try:
        created = rq.submit_open_hours(
            get_conn(), actor=account.as_actor(), requester_id=account.id,
            window_start=_as_utc(window_start), window_end=_as_utc(window_end),
            purpose=purpose, note=note,
        )
    except StockroomError as exc:
        return redirect("/requests/new/open_hours", error=str(exc))
    return redirect("/requests/mine", ok=f"Request #{created.id} submitted.")


@router.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(request: Request, request_id: int):
    """One request. Visible to its author and to staff, nobody else."""
    account = require_account(request)
    conn = get_conn()
    subject = rq.get_request(conn, request_id)
    if subject.requester_id != account.id and not account.is_staff:
        raise Forbidden("That request belongs to someone else.")
    # Staff only: a requester has no business seeing who else asked.
    overlaps = rq.overlapping_requests(conn, subject) if account.is_staff else []
    return page(
        request,
        "request_detail.html",
        subject=subject,
        item=service.get_item(conn, subject.item_id) if subject.item_id else None,
        overlaps=overlaps,
        competing_demand=rq.competing_demand(conn, subject) if overlaps else 0,
        now=db.utcnow(),
    )


@router.post("/requests/{request_id}/approve")
def approve(request: Request, request_id: int, note: str = Form("")):
    staff = require_staff(request)
    try:
        rq.approve(get_conn(), actor=staff.as_actor(), request_id=request_id,
                   decided_by_id=staff.id, note=note)
    except StockroomError as exc:
        return redirect(f"/requests/{request_id}", error=str(exc))
    return redirect(f"/requests/{request_id}", ok="Request approved.")


@router.post("/requests/{request_id}/decline")
def decline(request: Request, request_id: int, note: str = Form("")):
    staff = require_staff(request)
    try:
        rq.decline(get_conn(), actor=staff.as_actor(), request_id=request_id,
                   decided_by_id=staff.id, note=note)
    except StockroomError as exc:
        return redirect(f"/requests/{request_id}", error=str(exc))
    return redirect(f"/requests/{request_id}", ok="Request declined.")


@router.post("/requests/{request_id}/cancel")
def cancel(request: Request, request_id: int, next: str = Form("/requests/mine")):
    """Withdraw a request. The author, or staff on their behalf."""
    account = require_account(request)
    conn = get_conn()
    subject = rq.get_request(conn, request_id)
    if subject.requester_id != account.id and not account.is_staff:
        raise Forbidden("That request belongs to someone else.")
    try:
        rq.cancel(conn, actor=account.as_actor(), request_id=request_id,
                  by_account_id=account.id)
    except StockroomError as exc:
        return redirect(safe_path(next), error=str(exc))
    return redirect(safe_path(next), ok="Request cancelled.")


@router.post("/requests/{request_id}/fulfil")
def fulfil(request: Request, request_id: int, quantity: str = Form(""),
           due_at: str = Form("")):
    """Hand the equipment over -- this is the step that creates a real loan."""
    staff = require_staff(request)
    try:
        rq.fulfil_borrow(
            get_conn(), actor=staff.as_actor(), request_id=request_id,
            quantity=int(quantity) if quantity.strip() else None,
            due_at=_as_utc(due_at, end=True),
        )
    except (StockroomError, ValueError) as exc:
        return redirect(f"/requests/{request_id}", error=str(exc))
    return redirect(f"/requests/{request_id}", ok="Checked out.")


@router.post("/requests/{request_id}/fulfil-item")
def fulfil_item(request: Request, request_id: int, item_id: int = Form(...)):
    staff = require_staff(request)
    try:
        rq.fulfil_new_item(get_conn(), actor=staff.as_actor(),
                           request_id=request_id, item_id=item_id)
    except StockroomError as exc:
        return redirect(f"/requests/{request_id}", error=str(exc))
    return redirect(f"/requests/{request_id}", ok="Marked as added to the stockroom.")


# ---------------------------------------------------------------------------
# open hours
# ---------------------------------------------------------------------------


@router.post("/open-hours")
def add_open_hours(request: Request, window_start: str = Form(...),
                   window_end: str = Form(...), note: str = Form("")):
    staff = require_staff(request)
    try:
        rq.add_open_hours(get_conn(), actor=staff.as_actor(),
                          window_start=_as_utc(window_start),
                          window_end=_as_utc(window_end), note=note)
    except StockroomError as exc:
        return redirect("/requests", error=str(exc))
    return redirect("/requests", ok="Open hours published.")


@router.post("/open-hours/{slot_id}/cancel")
def cancel_open_hours(request: Request, slot_id: int):
    staff = require_staff(request)
    try:
        rq.cancel_open_hours(get_conn(), actor=staff.as_actor(), slot_id=slot_id)
    except StockroomError as exc:
        return redirect("/requests", error=str(exc))
    return redirect("/requests", ok="Open hours cancelled.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_limit(account_id: int) -> None:
    if not _submit_limit.allow(f"account:{account_id}"):
        raise Forbidden(
            "You have filed a lot of requests in the last hour. "
            "Please wait a little before submitting more."
        )


def _as_utc(value: str, *, end: bool = False) -> str | None:
    """Turn a browser datetime-local / date value into the stored UTC format.

    `<input type="datetime-local">` yields "2026-09-03T14:00" and
    `<input type="date">` yields "2026-09-03". Both are naive local times; the
    stockroom is one building in one timezone, so they are stored as given
    with a Z suffix rather than pretending to a precision the form does not
    carry. A bare date used as a deadline becomes end-of-day, so something due
    "Friday" is not overdue at midnight on Friday morning.
    """
    value = (value or "").strip()
    if not value:
        return None
    if len(value) == 10:            # a date with no time
        return f"{value}T23:59:59Z" if end else f"{value}T00:00:00Z"
    if len(value) == 16:            # datetime-local, no seconds
        return f"{value}:00Z"
    return value if value.endswith("Z") else f"{value}Z"
