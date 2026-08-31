"""Staff and admin management of accounts."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import accounts
from ..service import StockroomError
from .deps import get_conn, page, redirect, require_admin, require_staff

router = APIRouter()


@router.get("/accounts", response_class=HTMLResponse)
def list_accounts(request: Request, status: str = ""):
    """The directory, pending signups first -- that is the actionable list."""
    viewer = require_staff(request)
    conn = get_conn()
    return page(
        request,
        "accounts.html",
        viewer=viewer,
        accounts=accounts.list_accounts(conn, status=status or None),
        status_filter=status,
        roles=accounts.ROLES,
    )


@router.post("/accounts/{account_id}/approve")
def approve(request: Request, account_id: int):
    staff = require_staff(request)
    try:
        account = accounts.approve(
            get_conn(), actor=staff.as_actor(), account_id=account_id, approved_by=staff
        )
    except StockroomError as exc:
        return redirect("/accounts", error=str(exc))
    return redirect("/accounts", ok=f"Approved {account.name}. They can sign in now.")


@router.post("/accounts/{account_id}/status")
def set_status(request: Request, account_id: int, status: str = Form(...),
               reason: str = Form("")):
    staff = require_staff(request)
    try:
        account = accounts.set_status(
            get_conn(), actor=staff.as_actor(), account_id=account_id,
            status=status, reason=reason,
        )
    except StockroomError as exc:
        return redirect("/accounts", error=str(exc))
    return redirect("/accounts", ok=f"{account.name} is now {account.status}.")


@router.post("/accounts/{account_id}/role")
def set_role(request: Request, account_id: int, role: str = Form(...)):
    """Role changes are admin-only -- this is the privilege-granting path."""
    admin = require_admin(request)
    try:
        account = accounts.set_role(
            get_conn(), actor=admin.as_actor(), account_id=account_id, role=role
        )
    except StockroomError as exc:
        return redirect("/accounts", error=str(exc))
    return redirect("/accounts", ok=f"{account.name} is now {account.role}.")


@router.post("/accounts/{account_id}/revoke-sessions")
def revoke_sessions(request: Request, account_id: int):
    admin = require_admin(request)
    count = accounts.revoke_all_sessions(
        get_conn(), actor=admin.as_actor(), account_id=account_id
    )
    return redirect("/accounts", ok=f"Revoked {count} session(s).")
