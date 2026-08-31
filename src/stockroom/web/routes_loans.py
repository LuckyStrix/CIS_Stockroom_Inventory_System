"""Checkout and return."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import db, service
from ..service import StockroomError
from .deps import get_conn, page, redirect, require_staff

router = APIRouter()


@router.get("/loans", response_class=HTMLResponse)
def list_loans(request: Request, filter: str = ""):
    # Staff-only: this page lists who is holding what, across everyone. A
    # requester sees their own loans on /account instead.
    require_staff(request)
    conn = get_conn()
    return page(
        request,
        "loans.html",
        loans=service.list_loans(
            conn, open_only=True, overdue_only=(filter == "overdue")
        ),
        filter=filter,
        now=db.utcnow(),
        next_url=f"/loans?filter={filter}" if filter else "/loans",
    )


@router.post("/items/{item_id}/checkout")
def checkout(
    request: Request,
    item_id: int,
    person_email: str = Form(...),
    person_name: str = Form(""),
    quantity: int = Form(1),
    due_at: str = Form(""),
    note: str = Form(""),
):
    """Lend units of an item.

    The form offers a datalist of known people but accepts any email, so a
    new borrower is created inline rather than forcing a detour to the
    People page mid-checkout. If they are new and no name was typed, the
    local part of the email stands in until someone corrects it.
    """
    actor = require_staff(request).as_actor()
    conn = get_conn()

    existing = service.find_person_by_email(conn, person_email)
    name = person_name.strip() or (existing.name if existing else person_email.split("@")[0])

    try:
        loan = service.checkout(
            conn, actor=actor, item_id=item_id,
            person_name=name, person_email=person_email,
            quantity=quantity,
            # <input type="date"> gives YYYY-MM-DD; store end-of-day UTC so a
            # loan is not overdue at midnight on the morning it is due.
            due_at=f"{due_at}T23:59:59Z" if due_at.strip() else None,
            note=note,
        )
    except StockroomError as exc:
        return redirect(f"/items/{item_id}", error=str(exc))

    return redirect(
        f"/items/{item_id}",
        ok=f"Checked out {loan.quantity} x {loan.item_name} to {loan.person_name}.",
    )


@router.post("/loans/{loan_id}/return")
def return_loan(
    request: Request,
    loan_id: int,
    quantity: str = Form(""),
    note: str = Form(""),
    condition: str = Form(""),
    next: str = Form("/loans"),
):
    """Return all or part of a loan; ``next`` sends the user back where they were.

    ``condition`` marks what came back as not fit to lend again, in the same
    transaction. The moment a student hands over a dented lens is the only
    moment anyone will reliably record it, so it is one form, not two.
    """
    actor = require_staff(request).as_actor()
    try:
        loan = service.return_loan(
            get_conn(), actor=actor, loan_id=loan_id,
            quantity=int(quantity) if quantity.strip() else None,
            note=note,
            condition=condition.strip() or None,
        )
    except (StockroomError, ValueError) as exc:
        return redirect(next, error=str(exc))
    if condition.strip():
        return redirect(next, ok=(
            f"Returned {loan.item_name} from {loan.person_name}, marked "
            f"{condition.strip()}."
        ))
    return redirect(next, ok=f"Returned {loan.item_name} from {loan.person_name}.")
