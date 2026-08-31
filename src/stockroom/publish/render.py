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
from ..requests_service import list_open_hours
from ..service import audit_head, list_items, list_loans, summary

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
        # The head of the audit chain. Published here on purpose: the chain
        # detects an edited history, but anyone who can edit the database can
        # also recompute it. Copies of the head sitting outside the Pi -- in
        # this file, in /health, in every nightly backup -- are what make a
        # convincing rewrite expensive rather than trivial. It identifies
        # nothing and reveals nothing; it is a hash of hashes.
        "audit_head": audit_head(conn),
    }

    # Confirmed staffed windows. This is the payoff of the open-hours request
    # flow: "when can I actually come and collect this?" is answered without
    # anybody logging in.
    payload["open_hours"] = [
        {
            "start": slot.window_start,
            "end": slot.window_end,
            "note": slot.note,
        }
        for slot in list_open_hours(conn, upcoming_only=True, limit=10)
    ]

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


def _csp_hashes(html: str) -> list[str]:
    """SHA-256 hashes of every inline <script> and <style> block in the page.

    The public page is a single self-contained file that may be opened from
    disk or served by GitHub Pages, so there is no server to mint a per-request
    nonce. Hashes are the alternative the CSP spec provides for exactly this
    case: the policy names the scripts allowed to run, and anything injected
    later -- an item description that escaped, a tampered copy of the file --
    does not match and does not execute.
    """
    import base64
    import hashlib
    import re

    hashes = []
    for pattern in (
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
        r"<style[^>]*>(.*?)</style>",
    ):
        for block in re.findall(pattern, html, re.S):
            digest = hashlib.sha256(block.encode("utf-8")).digest()
            hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


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
        open_hours=payload["open_hours"],
        data_json=Markup(_json_for_script(payload["items"])),
        csp="",
    )

    # Two passes: the policy has to name the hashes of the very blocks the
    # first pass produced, so render once to obtain them and once to embed them.
    hashes = _csp_hashes(html)
    policy = "; ".join([
        "default-src 'none'",
        f"script-src {' '.join(h for h in hashes if h)}",
        f"style-src {' '.join(hashes)}",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])
    html = template.render(
        org=payload["organization"],
        generated_at=payload["generated_at"],
        summary=payload["summary"],
        show_borrowers=config.PUBLIC_SHOW_BORROWERS,
        open_hours=payload["open_hours"],
        data_json=Markup(_json_for_script(payload["items"])),
        csp=policy,
    )
    return {
        "index.html": html,
        "inventory.json": json.dumps(payload, indent=2, sort_keys=True) + "\n",
    }
