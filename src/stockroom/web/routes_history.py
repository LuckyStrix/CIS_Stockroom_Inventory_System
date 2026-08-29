"""The audit log, browsable and filterable."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import service
from .deps import get_conn, page

router = APIRouter()

PAGE_SIZE = 100


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    item_id: int | None = None,
    person_id: int | None = None,
    action: str = "",
    actor: str = "",
    offset: int = 0,
):
    conn = get_conn()
    offset = max(0, offset)

    # Fetch one extra row to learn whether an "Older" link is warranted,
    # without paying for a second COUNT query on every page view.
    events = service.list_events(
        conn, item_id=item_id, person_id=person_id,
        action=action or None, actor=actor or None,
        limit=PAGE_SIZE + 1, offset=offset,
    )
    has_more = len(events) > PAGE_SIZE
    events = events[:PAGE_SIZE]

    def page_url(new_offset: int) -> str:
        params = {"offset": max(0, new_offset)}
        if item_id:
            params["item_id"] = item_id
        if person_id:
            params["person_id"] = person_id
        if action:
            params["action"] = action
        if actor:
            params["actor"] = actor
        return "/history?" + urlencode(params)

    return page(
        request,
        "history.html",
        events=events,
        item=service.get_item(conn, item_id) if item_id else None,
        actions=service.list_actions(conn),
        action_filter=action,
        actor_filter=actor,
        offset=offset,
        limit=PAGE_SIZE,
        has_more=has_more,
        page_url=page_url,
    )
