"""Render the inventory to a self-contained public page.

The output is deliberately dumb: one HTML file with its CSS and JS inlined
and its data embedded as a JSON blob, plus the same data as a standalone
``inventory.json``. No build step, no CDN, no server. It opens correctly from
a file:// URL, from the Pi's own web server, or from GitHub Pages, and it
keeps working if the Pi is off.

**Privacy.** Borrower names and emails are omitted unless
``config.PUBLIC_SHOW_BORROWERS`` is explicitly turned on. The default page
answers "is one free?", not "who has it?".
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import config, db
from ..models import Item
from ..service import list_items, list_loans, summary

_TEMPLATE_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _item_payload(item: Item) -> dict[str, Any]:
    """The public view of one item. Nothing here identifies a borrower."""
    return {
        "name": item.name,
        "description": item.description,
        "barcode": item.barcode,
        "product_url": item.product_url,
        "location": item.location,
        "unit": item.unit,
        "shelf": item.shelf,
        "sub_location": item.sub_location,
        "quantity": item.quantity,
        "available": item.available,
        "out_qty": item.out_qty,
        "low_stock": item.is_low_stock,
        "status": item.status_label,
    }


def build_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble everything the public page and JSON feed need."""
    items = list_items(conn, include_archived=False)
    payload: dict[str, Any] = {
        "organization": config.ORG_NAME,
        "generated_at": db.utcnow(),
        "summary": summary(conn),
        "items": [_item_payload(i) for i in items],
    }

    if config.PUBLIC_SHOW_BORROWERS:
        # Opt-in only. See config.PUBLIC_SHOW_BORROWERS for the reasoning.
        payload["loans"] = [
            {
                "item": loan.item_name,
                "person": loan.person_name,
                "email": loan.person_email,
                "quantity": loan.quantity,
                "checked_out_at": loan.checked_out_at,
                "due_at": loan.due_at,
            }
            for loan in list_loans(conn, open_only=True)
        ]
    return payload


def _json_for_script(data: Any) -> str:
    """Serialize data for embedding in a ``<script>`` block.

    ``<``, ``>`` and ``&`` are emitted as ``\u003c``-style escapes. Those are
    valid JSON escapes that parse back to the original characters, and they
    make it impossible for item text to terminate the script element (a
    description containing ``</script>``) or to open an HTML comment.

    This has to be marked safe in the template, because Jinja's HTML
    autoescaping would otherwise turn the JSON into ``&#34;``-entities --
    and entities are *not* decoded inside a script element, so JSON.parse
    would fail and the page would render an empty table.
    """
    return (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_json(conn: sqlite3.Connection) -> str:
    """The machine-readable feed."""
    return json.dumps(build_payload(conn), indent=2, sort_keys=True) + "\n"


def render_site(conn: sqlite3.Connection) -> dict[str, str]:
    """Render every public file. Returns ``{filename: contents}``.

    Returning a mapping rather than writing to disk keeps rendering pure and
    testable, and lets each publisher decide where the bytes go.
    """
    payload = build_payload(conn)
    template = _env.get_template("public.html")
    html = template.render(
        org=payload["organization"],
        generated_at=payload["generated_at"],
        summary=payload["summary"],
        show_borrowers=config.PUBLIC_SHOW_BORROWERS,
        data_json=Markup(_json_for_script(payload["items"])),
    )
    return {
        "index.html": html,
        "inventory.json": json.dumps(payload, indent=2, sort_keys=True) + "\n",
    }
