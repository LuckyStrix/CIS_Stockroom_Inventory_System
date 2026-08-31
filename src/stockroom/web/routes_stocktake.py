"""Walking the shelves with a scanner."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import service, stocktake
from ..service import StockroomError
from .deps import get_conn, page, redirect, require_staff

router = APIRouter()


@router.get("/stocktake", response_class=HTMLResponse)
def index(request: Request):
    """The one in progress, or the form to start one."""
    require_staff(request)
    conn = get_conn()
    current = stocktake.open_stocktake(conn)
    return page(
        request,
        "stocktake.html",
        current=current,
        progress=stocktake.reconcile(conn, current.id) if current else None,
        history=stocktake.list_stocktakes(conn),
        storage_units=service.list_storage_units(conn),
    )


@router.post("/stocktake")
def start(request: Request, scope_unit: str = Form(""), note: str = Form("")):
    actor = require_staff(request).as_actor()
    try:
        stocktake.start_stocktake(get_conn(), actor=actor,
                                  scope_unit=scope_unit or None, note=note)
    except StockroomError as exc:
        return redirect("/stocktake", error=str(exc))
    return redirect("/stocktake", ok="Counting. Scan what is on the shelves.")


@router.post("/stocktake/{stocktake_id}/scan")
def scan(request: Request, stocktake_id: int, code: str = Form("")):
    """Record one thing seen on the shelf, then come straight back for more."""
    actor = require_staff(request).as_actor()
    try:
        _, message = stocktake.record_scan(get_conn(), actor=actor,
                                           stocktake_id=stocktake_id, code=code)
    except StockroomError as exc:
        return redirect("/stocktake", error=str(exc))
    return redirect("/stocktake", ok=message)


@router.post("/stocktake/{stocktake_id}/finish")
def finish(request: Request, stocktake_id: int):
    actor = require_staff(request).as_actor()
    try:
        stocktake.finish_stocktake(get_conn(), actor=actor,
                                   stocktake_id=stocktake_id)
    except StockroomError as exc:
        return redirect("/stocktake", error=str(exc))
    return redirect(f"/stocktake/{stocktake_id}", ok="Counted. Here is what it found.")


@router.post("/stocktake/{stocktake_id}/abandon")
def abandon(request: Request, stocktake_id: int, reason: str = Form("")):
    actor = require_staff(request).as_actor()
    try:
        stocktake.abandon_stocktake(get_conn(), actor=actor,
                                    stocktake_id=stocktake_id, reason=reason)
    except StockroomError as exc:
        return redirect("/stocktake", error=str(exc))
    return redirect("/stocktake", ok="Abandoned. The scans so far are kept.")


@router.get("/stocktake/{stocktake_id}", response_class=HTMLResponse)
def report(request: Request, stocktake_id: int):
    require_staff(request)
    conn = get_conn()
    return page(
        request,
        "stocktake_report.html",
        result=stocktake.reconcile(conn, stocktake_id),
    )


@router.post("/stocktake/{stocktake_id}/mark-missing")
def mark_missing(request: Request, stocktake_id: int, item_id: int = Form(...),
                 quantity: int = Form(...)):
    """Turn a shortfall into a `missing` hold.

    Never automatic. The overwhelmingly common cause of a missing scan is a
    missed scan, and a stocktake that quietly wrote off stock every time
    somebody skipped a shelf would be worse than no stocktake at all.
    """
    actor = require_staff(request).as_actor()
    conn = get_conn()
    try:
        item = service.get_item(conn, item_id)
        service.open_hold(
            conn, actor=actor, item_id=item_id, state="missing",
            quantity=quantity,
            note=f"Not found during the stocktake on {stocktake_id}",
        )
    except StockroomError as exc:
        return redirect(f"/stocktake/{stocktake_id}", error=str(exc))
    return redirect(f"/stocktake/{stocktake_id}",
                    ok=f"{quantity} x {item.name} recorded as missing.")
