"""People: the directory, and what each person is holding."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import db, service
from ..service import StockroomError
from .deps import get_conn, page, redirect, require_staff

router = APIRouter()


@router.get("/people", response_class=HTMLResponse)
def list_people(request: Request):
    # The directory carries names, emails and borrowing history: staff only.
    require_staff(request)
    conn = get_conn()
    open_loans = service.list_loans(conn, open_only=True)

    # One pass over open loans rather than two queries per person.
    counts: dict[int, list[int]] = {}
    for loan in open_loans:
        tally = counts.setdefault(loan.person_id, [0, 0])
        tally[0] += 1
        tally[1] += loan.quantity

    rows = [
        {
            "person": person,
            "open_loans": counts.get(person.id, [0, 0])[0],
            "open_units": counts.get(person.id, [0, 0])[1],
        }
        for person in service.list_people(conn, include_inactive=True)
    ]
    # People holding something float to the top -- that is what staff look for.
    rows.sort(key=lambda r: (-r["open_loans"], r["person"].name.lower()))
    return page(
        request,
        "people.html",
        people=rows,
        duplicates=service.possible_duplicates(conn),
    )


@router.post("/people")
def create_person(request: Request, name: str = Form(...), email: str = Form(...)):
    actor = require_staff(request).as_actor()
    try:
        person = service.create_person(get_conn(), actor=actor, name=name, email=email)
    except StockroomError as exc:
        return redirect("/people", error=str(exc))
    return redirect(f"/people/{person.id}", ok=f"Added {person.name}.")


@router.post("/people/merge")
def merge_people(request: Request, keep_id: int = Form(...),
                 merge_id: int = Form(...), reason: str = Form("")):
    """Fold one duplicate person record into another."""
    actor = require_staff(request).as_actor()
    try:
        kept = service.merge_people(get_conn(), actor=actor, keep_id=keep_id,
                                    merge_id=merge_id, reason=reason)
    except StockroomError as exc:
        return redirect("/people", error=str(exc))
    return redirect(f"/people/{kept.id}",
                    ok=f"Merged into {kept.name}. Their loans are all here now.")


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_detail(request: Request, person_id: int):
    require_staff(request)
    conn = get_conn()
    person = service.get_person(conn, person_id)
    # A merged record is not a dead end: send anyone who follows an old link
    # to the record that now holds their history.
    if person.is_merged:
        return redirect(
            f"/people/{person.merged_into_id}",
            ok=f"{person.name} was merged into this record.",
        )
    loans = service.list_loans(conn, person_id=person_id)
    open_loans = [l for l in loans if l.is_open]
    return page(
        request,
        "person_detail.html",
        person=person,
        open_loans=open_loans,
        open_units=sum(l.quantity for l in open_loans),
        past_loans=[l for l in loans if not l.is_open][:60],
        events=service.list_events(conn, person_id=person_id, limit=50),
        now=db.utcnow(),
        next_url=f"/people/{person_id}",
    )
