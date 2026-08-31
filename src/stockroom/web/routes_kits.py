"""Kits: named bundles staff can drop into the counter basket in one click."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import kits, service
from ..service import StockroomError
from .deps import get_conn, page, redirect, require_staff

router = APIRouter()


@router.get("/kits", response_class=HTMLResponse)
def list_kits(request: Request, show: str = ""):
    require_staff(request)
    conn = get_conn()
    all_kits = kits.list_kits(conn, include_archived=(show == "archived"))
    if show == "archived":
        all_kits = [k for k in all_kits if k.is_archived]
    return page(request, "kits.html", kits=all_kits, show=show)


@router.post("/kits")
def create_kit(request: Request, name: str = Form(...),
               description: str = Form("")):
    actor = require_staff(request).as_actor()
    try:
        kit = kits.create_kit(get_conn(), actor=actor, name=name,
                              description=description)
    except StockroomError as exc:
        return redirect("/kits", error=str(exc))
    return redirect(f"/kits/{kit.id}", ok=f"Created {kit.name}. Now add its contents.")


@router.get("/kits/{kit_id}", response_class=HTMLResponse)
def kit_detail(request: Request, kit_id: int):
    require_staff(request)
    conn = get_conn()
    return page(
        request,
        "kit_detail.html",
        kit=kits.get_kit(conn, kit_id),
        items=service.list_items(conn),
    )


@router.post("/kits/{kit_id}/edit")
def edit_kit(request: Request, kit_id: int, name: str = Form(...),
             description: str = Form("")):
    actor = require_staff(request).as_actor()
    try:
        kits.update_kit(get_conn(), actor=actor, kit_id=kit_id, name=name,
                        description=description)
    except StockroomError as exc:
        return redirect(f"/kits/{kit_id}", error=str(exc))
    return redirect(f"/kits/{kit_id}", ok="Saved.")


@router.post("/kits/{kit_id}/contents")
def set_contents(request: Request, kit_id: int, item_id: list[str] = Form([]),
                 quantity: list[str] = Form([]), add_item: str = Form(""),
                 add_quantity: str = Form("1")):
    """Replace the kit's contents with what the form shows.

    A quantity of zero is how the form removes a line, which is why this is a
    wholesale replace rather than a diff -- what the operator sees on screen
    is exactly what gets saved.
    """
    actor = require_staff(request).as_actor()
    lines: list[tuple[int, int]] = []
    for raw_id, raw_qty in zip(item_id, quantity):
        try:
            lines.append((int(raw_id), int(raw_qty)))
        except (TypeError, ValueError):
            continue
    if add_item.strip():
        try:
            lines.append((int(add_item), int(add_quantity or 1)))
        except (TypeError, ValueError):
            pass

    try:
        kits.set_kit_contents(get_conn(), actor=actor, kit_id=kit_id, lines=lines)
    except StockroomError as exc:
        return redirect(f"/kits/{kit_id}", error=str(exc))
    return redirect(f"/kits/{kit_id}", ok="Contents saved.")


@router.post("/kits/{kit_id}/archive")
def archive_kit(request: Request, kit_id: int):
    actor = require_staff(request).as_actor()
    try:
        kits.archive_kit(get_conn(), actor=actor, kit_id=kit_id)
    except StockroomError as exc:
        return redirect(f"/kits/{kit_id}", error=str(exc))
    return redirect("/kits", ok="Archived.")


@router.post("/kits/{kit_id}/restore")
def restore_kit(request: Request, kit_id: int):
    actor = require_staff(request).as_actor()
    try:
        kits.restore_kit(get_conn(), actor=actor, kit_id=kit_id)
    except StockroomError as exc:
        return redirect("/kits", error=str(exc))
    return redirect(f"/kits/{kit_id}", ok="Restored.")
