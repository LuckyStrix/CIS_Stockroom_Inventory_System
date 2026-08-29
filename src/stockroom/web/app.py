"""The FastAPI application.

Run it with::

    uvicorn stockroom.web.app:app --host 0.0.0.0 --port 8000

or, in production, via the systemd unit in ``deploy/stockroom.service``.

Composition, in order:

* the database is created/upgraded at startup, so a fresh Pi needs no
  manual init step;
* the publish worker is installed, so every committed change rebuilds the
  public page in the background;
* ``/public`` serves that generated page, and ``/static`` the UI's CSS;
* the route modules are mounted.

There is no authentication. That is deliberate for this phase -- the service
is bound to the stockroom LAN and identity is self-declared for the audit log
(see ``web/deps.py`` and ``docs/sso-integration.md``).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, config, db, service
from ..publish import worker as publish_worker
from ..service import Actor, NotFound, StockroomError
from . import routes_history, routes_items, routes_loans, routes_people
from .deps import _IdentifyRedirect, page, redirect, set_actor_cookie, templates

log = logging.getLogger("stockroom")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    db.init_db()
    config.PUBLISH_DIR.mkdir(parents=True, exist_ok=True)

    worker = publish_worker.install(db_path=config.DB_PATH)
    # Render once at boot so /public is never a 404 on a fresh install, and
    # so a page edited or deleted by hand is restored on restart.
    try:
        worker.publish()
    except Exception:
        log.exception("initial publish failed (continuing)")

    log.info("stockroom %s ready · db=%s · publish=%s",
             __version__, config.DB_PATH, config.PUBLISH_DIR)
    yield
    worker.shutdown()


app = FastAPI(
    title="CIS Stockroom Inventory",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
)


# ---------------------------------------------------------------------------
# template filters
# ---------------------------------------------------------------------------
def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def fmt_datetime(value: str | None) -> str:
    parsed = _parse(value)
    return parsed.strftime("%d %b %Y, %H:%M") if parsed else "—"


def fmt_date(value: str | None) -> str:
    parsed = _parse(value)
    return parsed.strftime("%d %b %Y") if parsed else "—"


def show(value) -> str:
    """Render a diff value readably: None becomes an em dash, not 'None'."""
    if value is None or value == "":
        return "—"
    return str(value)


def event_class(action: str) -> str:
    """CSS class driving the colour of a timeline dot."""
    if action.endswith(".create"):
        return "create"
    if action == "loan.checkout":
        return "checkout"
    if action.startswith("loan.") and "return" in action:
        return "ret"
    if action in {"item.archive"}:
        return "archive"
    return ""


templates.env.filters["datetime"] = fmt_datetime
templates.env.filters["date"] = fmt_date
templates.env.filters["show"] = show
templates.env.filters["event_class"] = event_class


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------
@app.exception_handler(_IdentifyRedirect)
async def _identify(request: Request, exc: _IdentifyRedirect):
    """Anyone about to change something must first say who they are."""
    return RedirectResponse(f"/whoami?next={exc.next}", status_code=303)


@app.exception_handler(NotFound)
async def _not_found(request: Request, exc: NotFound):
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 404, "title": "Not found", "message": str(exc),
         "actor": None, "org": config.ORG_NAME, "path": request.url.path,
         "summary": service.summary(db.connect()), "ok": "", "error": ""},
        status_code=404,
    )


@app.exception_handler(StockroomError)
async def _stockroom_error(request: Request, exc: StockroomError):
    """A rejected-but-expected operation, surfaced as a message not a crash."""
    referer = request.headers.get("referer", "/")
    return redirect(referer, error=str(exc))


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/api", "/static", "/public")):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        request, "error.html",
        {"code": exc.status_code, "title": "Something went wrong",
         "message": exc.detail, "actor": None, "org": config.ORG_NAME,
         "path": request.url.path, "summary": service.summary(db.connect()),
         "ok": "", "error": ""},
        status_code=exc.status_code,
    )


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
@app.get("/whoami", response_class=HTMLResponse)
def whoami_form(request: Request, next: str = "/"):
    return page(request, "whoami.html", next_url=next or "/")


@app.post("/whoami")
def whoami_save(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    next: str = Form("/"),
):
    actor = Actor(name=name.strip(), email=email.strip().lower())
    if not actor.name:
        return redirect("/whoami", error="Please enter a name.")
    # Only ever redirect within this app -- never to an absolute URL supplied
    # in the form.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = redirect(target, ok=f"Hello, {actor.name}.")
    set_actor_cookie(response, actor)
    return response


# ---------------------------------------------------------------------------
# operational endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Liveness probe: confirms the database answers and reports scale."""
    conn = db.connect()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "database": str(config.DB_PATH),
            "schema_version": db.get_meta(conn, "schema_version"),
            **service.summary(conn),
        }
    )


@app.post("/publish")
def republish():
    """Force an immediate rebuild of the public page."""
    publish_worker.publish_now(config.DB_PATH)
    return redirect("/", ok="Public page rebuilt.")


# ---------------------------------------------------------------------------
# mounts
# ---------------------------------------------------------------------------
app.include_router(routes_items.router)
app.include_router(routes_loans.router)
app.include_router(routes_people.router)
app.include_router(routes_history.router)

app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")


# The generated public page, served by the Pi itself.
#
# This is a route rather than a StaticFiles mount because a mount binds its
# directory at import time, and config.PUBLISH_DIR is resolved from the
# environment. Reading the setting per request keeps the served directory and
# the published directory from ever drifting apart.
_PUBLIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


@app.get("/public", include_in_schema=False)
def public_root_redirect():
    return RedirectResponse("/public/", status_code=307)


@app.get("/public/{path:path}", include_in_schema=False)
def public_page(path: str = ""):
    """Serve the generated public site."""
    root = config.PUBLISH_DIR.resolve()
    target = (root / (path or "index.html")).resolve()

    # Refuse anything that escapes the publish directory.
    if not target.is_relative_to(root):
        raise StarletteHTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise StarletteHTTPException(
            status_code=404,
            detail="The public page has not been generated yet. Run `stockroom publish`.",
        )

    return FileResponse(
        target,
        media_type=_PUBLIC_TYPES.get(target.suffix, "application/octet-stream"),
        # The page is rewritten on every change; never let a browser cache it.
        headers={"Cache-Control": "no-cache"},
    )
