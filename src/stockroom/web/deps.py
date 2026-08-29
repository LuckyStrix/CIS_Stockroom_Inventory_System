"""Shared request plumbing: the current operator, templates, flash messages.

    ======================================================================
    THE SSO SEAM: `current_actor()` below is the ONE function that decides
    who is performing an action. Everything else -- routes, service layer,
    audit log -- takes an Actor and does not care where it came from.
    Swapping the cookie for a Shibboleth session is a change to this
    function and nothing else. See docs/sso-integration.md.
    ======================================================================

Today identity is self-declared: the browser is asked "who are you?" once and
the answer is kept in a cookie. That is appropriate for a trusted stockroom
LAN and useless as a security control, which is the documented, deliberate
trade-off for this phase -- the point of recording the actor now is
accountability in the audit log, not access control.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, db, service
from ..service import Actor

_PACKAGE_DIR = Path(__file__).resolve().parent.parent

templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))

ACTOR_COOKIE = "stockroom_operator"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # a year; this is a convenience, not a session

# Headers a Shibboleth SP (mod_shib) puts in front of the app. Read here so
# that the day SSO is switched on, the app picks the identity up with no code
# change -- see docs/sso-integration.md.
_SSO_NAME_HEADERS = ("x-shib-displayname", "displayname", "x-remote-user-name")
_SSO_MAIL_HEADERS = ("x-shib-mail", "mail", "x-remote-user-email")


def get_conn() -> sqlite3.Connection:
    """The database connection for this request (thread-local)."""
    return db.connect()


def _header(request: Request, names: tuple[str, ...]) -> str:
    for name in names:
        value = request.headers.get(name)
        if value:
            return value.strip()
    return ""


def current_actor(request: Request) -> Actor | None:
    """Who is making this request, or None if they have not identified.

    Resolution order:

    1. Shibboleth/SP headers, if the app is running behind one. Trusted
       because only the SP can set them once it is deployed correctly.
    2. The self-declared cookie (today's default).
    3. Nobody -- the caller redirects to the "who are you?" page.
    """
    email = _header(request, _SSO_MAIL_HEADERS)
    remote_user = request.headers.get("x-remote-user", "").strip()
    if email or remote_user:
        name = _header(request, _SSO_NAME_HEADERS) or email or remote_user
        return Actor(name=name, email=email or remote_user)

    raw = request.cookies.get(ACTOR_COOKIE, "")
    if not raw:
        return None
    name, _, mail = raw.partition("|")
    name = name.strip()
    return Actor(name=name, email=mail.strip()) if name else None


def require_actor(request: Request) -> Actor:
    """The current operator, or raise a redirect to the identify page."""
    actor = current_actor(request)
    if actor is None:
        raise _IdentifyRedirect(request)
    return actor


class _IdentifyRedirect(Exception):
    """Raised when an unidentified operator hits a page that records changes.

    Handled by an exception handler in app.py, which sends them to /whoami
    with a ?next= pointing back here.
    """

    def __init__(self, request: Request) -> None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        self.next = target
        super().__init__("operator not identified")


def set_actor_cookie(response, actor: Actor) -> None:
    response.set_cookie(
        ACTOR_COOKIE,
        f"{actor.name}|{actor.email}",
        max_age=COOKIE_MAX_AGE,
        httponly=False,   # harmless here, and lets the page show who you are
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# flash messages
#
# Carried in the query string rather than server-side session state: the app
# is a single process with no session store, and the messages are short and
# non-sensitive ("Checked out 2 x Canon EOS R5").
# ---------------------------------------------------------------------------
def redirect(path: str, *, ok: str = "", error: str = "") -> RedirectResponse:
    """Redirect after a POST, carrying a flash message."""
    separator = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{separator}ok={quote(ok)}"
    elif error:
        path = f"{path}{separator}error={quote(error)}"
    return RedirectResponse(path, status_code=303)


def page(request: Request, template: str, **context):
    """Render a template with the context every page needs."""
    conn = get_conn()
    context.setdefault("ok", request.query_params.get("ok", ""))
    context.setdefault("error", request.query_params.get("error", ""))
    return templates.TemplateResponse(
        request,
        template,
        {
            "actor": current_actor(request),
            "org": config.ORG_NAME,
            "path": request.url.path,
            "summary": service.summary(conn),
            **context,
        },
    )
