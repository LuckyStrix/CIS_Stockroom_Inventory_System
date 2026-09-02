"""The FastAPI application.

Run behind nginx, which terminates TLS::

    uvicorn stockroom.web.app:app --host 127.0.0.1 --port 8000 --proxy-headers

Binding to loopback is not incidental. The app is never directly reachable
from the network: nginx is the only thing that can talk to it, which is what
makes it safe to trust `X-Forwarded-Proto` and `X-Forwarded-For` in deps.py.

Security posture, in one place:

* **Authentication** -- server-side sessions; every route except a short
  explicit list requires one (`deps.PUBLIC_PATHS`).
* **CSRF** -- a synchroniser token is required on every unsafe method,
  enforced by middleware rather than per route, so a new POST cannot forget it.
* **CSP** -- a per-request nonce; no inline handlers anywhere in the templates.
* **No open redirects** -- every caller-supplied destination goes through
  `deps.safe_path`.

What this deliberately is *not*: internet-facing. See docs/security.md.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from .. import __version__, accounts, config, db, service
from ..publish import worker as publish_worker
from ..service import NotFound, StockroomError
from . import (
    routes_accounts,
    routes_admin,
    routes_counter,
    routes_kits,
    routes_stocktake,
    routes_auth,
    routes_history,
    routes_items,
    routes_loans,
    routes_people,
    routes_requests,
    routes_sso,
)
from .deps import (
    CSRFError,
    require_account,
    Forbidden,
    _LoginRedirect,
    is_csrf_exempt,
    is_public_path,
    login_url,
    utc_to_local,
    redirect,
    safe_path,
    set_csrf_cookie,
    templates,
    verify_csrf,
)

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
    try:
        worker.publish()
    except Exception:
        log.exception("initial publish failed (continuing)")

    if not accounts.list_accounts(db.connect(), role="admin"):
        log.warning(
            "No administrator account exists. Create one with: "
            "stockroom user create --admin"
        )

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
# the Host check
# ---------------------------------------------------------------------------


def _requested_host(headers: Headers) -> str:
    """The hostname from the Host header, without the port.

    IPv6 arrives bracketed -- `[fe80::1]:443` -- so splitting on ":" the way
    the obvious one-liner does turns it into "[fe80", which then matches
    nothing. Brackets are stripped: the allow list holds bare addresses.
    """
    host = headers.get("host", "").strip().lower()
    if host.startswith("["):
        host, _, _ = host.partition("]")
        return host[1:]
    return host.split(":")[0]


def _host_is_allowed(host: str) -> bool:
    if not host:
        return False
    if "*" in config.ALLOWED_HOSTS or host in config.ALLOWED_HOSTS:
        return True
    if config.ALLOW_IP_HOSTS:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
    # `*.example.edu` matches a subdomain, as Starlette's own middleware does.
    return any(
        pattern.startswith("*.") and host.endswith(pattern[1:])
        for pattern in config.ALLOWED_HOSTS
    )


class HostCheckMiddleware:
    """Reject requests carrying an unexpected Host header.

    This replaces Starlette's TrustedHostMiddleware, which does the same job.
    It was swapped out for what it says when it refuses: a bare 400 reading
    "Invalid host header", with no hint as to which header, which hosts are
    acceptable or where the list is configured. On a stockroom Pi that is a
    whole afternoon -- the site simply stops working from every device at once,
    and nothing on the page or in the log says why.

    So: the same check, plus IP addresses (see config.ALLOW_IP_HOSTS), plus an
    answer that tells whoever is standing there what to do about it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        host = _requested_host(Headers(scope=scope))
        if _host_is_allowed(host):
            await self.app(scope, receive, send)
            return

        # Never echo a raw header back verbatim: this response is generated
        # above the middleware that sets X-Content-Type-Options, so a sniffing
        # browser is the one thing standing between it and rendered markup.
        shown = "".join(c for c in host if c.isalnum() or c in ".-:")[:60] or "(none)"
        log.warning("rejected request with Host %r (allowed: %s)",
                    host, ",".join(config.ALLOWED_HOSTS))
        body = (
            f"Invalid host header: {shown}\n\n"
            f"This server answers to: {', '.join(config.ALLOWED_HOSTS)}"
            + (" (and any IP address)" if config.ALLOW_IP_HOSTS else "")
            + "\n\n"
            "Add the name you used to STOCKROOM_ALLOWED_HOSTS in "
            "/etc/stockroom.env and restart:\n"
            "    sudo systemctl restart stockroom\n"
        )
        response = PlainTextResponse(body, status_code=400)
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# middleware
#
# Defined here, registered together at the bottom of this section -- see the
# comment on that block. The order they run in is not the order they appear in.
# ---------------------------------------------------------------------------


async def security_middleware(request: Request, call_next):
    """CSRF enforcement, a CSP nonce, and security headers, for every request.

    Doing CSRF here rather than as a per-route dependency is the point: this
    cannot be forgotten when someone adds a route, and `tests/test_security.py`
    walks every POST in the application to prove it.
    """
    request.state.csp_nonce = secrets.token_urlsafe(16)

    def finish(response):
        """Everything that must happen on the way out, however we got here.

        The two refusals below return without ever calling the route, and they
        return *rendered HTML*. Returning them directly skipped this, so the
        403 "Form expired" and 413 "Too large" pages went to the browser with
        no CSP, no X-Frame-Options and no nosniff -- the pages most likely to
        be reached by a hostile request were the only ones with no hardening
        on them.
        """
        _apply_security_headers(request, response)
        # A page rendered for an anonymous visitor may have minted a CSRF
        # token for its form; this is where that token reaches the browser.
        set_csrf_cookie(response, request)
        return response

    if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
        # Refuse an oversized body before reading it. This middleware reads the
        # whole body into memory to find the CSRF field, so without a ceiling a
        # single large POST is a trivial way to exhaust a Pi's RAM. nginx caps
        # this in production; development has no nginx in front of it.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > config.MAX_UPLOAD_BYTES:
            return finish(_too_large(request))
        try:
            submitted = await _submitted_csrf(request)
        except _BodyTooLarge:
            # A chunked request carries no Content-Length, so the header check
            # above sees nothing to compare and waves it through. nginx caps
            # the body at 8m in production and is the real defence; this is
            # what stops the development server, which has no nginx in front
            # of it, being asked to hold an unbounded body in memory.
            return finish(_too_large(request))
        # The size cap and the body read above still happen for every path,
        # including the exempt one: _read_body_capped is what bounds a chunked
        # body, and stashing the body is what lets the route read its form
        # afterwards. Only the token check is skipped.
        #
        # /sso/acs is the only path that skips it, and it does not go
        # unprotected -- deps.require_saml_handshake is a stronger control
        # applied in its place, and that route still answers 403 to a request
        # it refuses, exactly like every other POST here.
        if not is_csrf_exempt(request.url.path):
            try:
                verify_csrf(request, submitted)
            except CSRFError as exc:
                return finish(_error_page(request, 403, "Form expired", str(exc)))

    return finish(await call_next(request))


# How far into a multipart body to look for the CSRF field. Generous enough
# for any number of ordinary text fields ahead of it, small enough that a 9 MB
# photo is never scanned. Enforced by test_the_csrf_field_comes_first.
_MULTIPART_SCAN_BYTES = 16 * 1024


class _BodyTooLarge(Exception):
    """The body passed MAX_UPLOAD_BYTES while it was being read."""


def _too_large(request: Request):
    return _error_page(
        request, 413, "Too large",
        f"That upload is larger than the "
        f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
    )


async def _read_body_capped(request: Request) -> bytes:
    """Read the whole body, refusing to buffer more than the limit.

    `request.body()` reads to the end whatever the size, so checking the
    length afterwards has already spent the memory -- which is all the
    Content-Length header check above can do for a chunked request, since
    those carry no such header to check.

    Accumulating the stream here and stopping at the ceiling is what actually
    bounds it. The result is stashed on `_body` exactly as `body()` would do
    it -- `Request.stream()` looks there first -- so everything downstream,
    including `form()`, sees a body that was read normally. That equivalence
    is what `test_csrf_middleware_does_not_eat_the_request_body` checks.
    """
    if hasattr(request, "_body"):
        return request._body

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.MAX_UPLOAD_BYTES:
            raise _BodyTooLarge
        chunks.append(chunk)

    request._body = b"".join(chunks)
    return request._body


async def _submitted_csrf(request: Request) -> str:
    """Pull the CSRF field out of the request body.

    Reads the *raw* body rather than calling `request.form()`. That is not a
    style preference: Starlette's middleware only replays the body downstream
    when `body()` was used to read it. Calling `form()` here consumes the
    stream without caching it, and every route below would then see an empty
    form and fail validation. Verified by
    `test_csrf_middleware_does_not_eat_the_request_body`.
    """
    body = await _read_body_capped(request)
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qs

        values = parse_qs(body.decode("utf-8", "replace")).get("_csrf", [])
        return values[0] if values else ""

    if content_type.startswith("multipart/form-data"):
        # Find the one field we need rather than running a full multipart parse
        # here, which would consume the stream that the route below still has
        # to read.
        #
        # Photo uploads made this delicate. The body now contains binary JPEG
        # data, so the search is bounded to the first slice of it: every form
        # in this application puts `_csrf` first (there is a test for that),
        # and scanning megabytes of image bytes with a regex for every upload
        # is work with no possible payoff. `re.S` is gone for the same reason
        # a bound was added -- `.` must not run away across binary content.
        import re

        head = body[:_MULTIPART_SCAN_BYTES]
        match = re.search(rb'name="_csrf"\r\n\r\n([^\r\n]*)\r\n', head)
        return match.group(1).decode("utf-8", "replace") if match else ""

    return ""


def _apply_security_headers(request: Request, response) -> None:
    nonce = getattr(request.state, "csp_nonce", "")
    is_html = response.headers.get("content-type", "").startswith("text/html")

    # The generated public page brings its own policy and must not be given
    # this one as well.
    #
    # It is a static file: written once by the publisher, served to many
    # requests, and expected to work from a USB stick and from GitHub Pages
    # where there is no server at all. So it cannot carry a per-request nonce,
    # and publish/render._csp_hashes gives it a <meta> CSP built from SHA-256
    # hashes of its own inline blocks instead -- a stricter policy than this
    # one, and the reason test_web skips public.html in the nonce check.
    #
    # A browser enforces every policy it is handed and takes the intersection.
    # Sending the nonce policy alongside the page's hash policy therefore
    # allowed nothing: the page rendered unstyled with an empty table, silently,
    # on every path except nginx (which serves /public/ from disk and adds no
    # CSP of its own). Leaving it off here makes all four delivery routes --
    # this app, nginx, file:// and Pages -- behave identically.
    #
    # Every other header below still applies.
    serves_generated_page = request.url.path.startswith("/public")

    if is_html and not serves_generated_page:
        # 'self' plus a nonce: no inline handlers, no third-party origins, and
        # nothing may frame this app or rewrite its base URL.
        response.headers["Content-Security-Policy"] = "; ".join([
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
        ])

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

    # Only assert HSTS when the request actually arrived over TLS -- sending it
    # over plain HTTP is meaningless, and on a hostname that later has to serve
    # something else it is a lasting nuisance.
    if request.url.scheme == "https" or \
            request.headers.get("x-forwarded-proto", "").lower() == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"


async def require_authentication(request: Request, call_next):
    """Deny anonymous access to everything not explicitly public.

    A deny-by-default gate, so forgetting a guard on a new route fails closed
    rather than silently exposing it.
    """
    if not is_public_path(request.url.path):
        from .deps import current_account

        if current_account(request) is None:
            if request.method == "GET":
                target = request.url.path
                if request.url.query:
                    target += "?" + request.url.query
                return RedirectResponse(login_url(target), status_code=303)
            return _error_page(
                request, 401, "Sign in required",
                "Your session has expired. Sign in and try again.",
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware registration. READ THIS BEFORE ADDING ONE.
#
# Starlette inserts each new middleware at the FRONT of the list, so this
# block reads backwards: the last line below is the outermost layer, and a
# request passes through it first.
#
#     HostCheckMiddleware        reject a forged Host before doing any work
#       security_middleware      CSRF in, security headers and CSP nonce out
#         require_authentication deny-by-default for anything not public
#           the routes
#
# It used to be exactly upside down, because three separate `@app.middleware`
# decorators register in the order their `def` executes and nothing said so.
# Two things were wrong with that:
#
#   * `require_authentication` was outermost, so its 401 page and its
#     /login redirect returned WITHOUT passing back out through
#     security_middleware -- no CSP, no X-Frame-Options, no nosniff, and no
#     CSRF cookie for the login form it was sending people to;
#   * the Host check was innermost, so a request to a protected path with a
#     forged Host was answered with a 303 to /login and never reached it.
#     Both Host tests use /health, which is public, so neither noticed.
#
# One consequence of the new order is deliberate: an anonymous POST to a
# protected path with no CSRF token now gets 403 "Form expired" rather than
# 401 "Sign in required", because CSRF is now the outer of the two checks.
# Both are correct refusals and both now carry the security headers.
# ---------------------------------------------------------------------------
app.add_middleware(BaseHTTPMiddleware, dispatch=require_authentication)
app.add_middleware(BaseHTTPMiddleware, dispatch=security_middleware)
app.add_middleware(HostCheckMiddleware)


# ---------------------------------------------------------------------------
# template filters and globals
# ---------------------------------------------------------------------------
def fmt_datetime(value: str | None) -> str:
    """A stored UTC timestamp, printed on the stockroom's own wall clock.

    Everything is stored in UTC and nothing here used to convert it back, so
    a checkout at 2pm in Rochester was displayed as 18:00 -- correct data,
    read out in a timezone nobody in the building is standing in.
    """
    parsed = utc_to_local(value)
    return parsed.strftime("%d %b %Y, %H:%M") if parsed else "\u2014"


def fmt_date(value: str | None) -> str:
    parsed = utc_to_local(value)
    return parsed.strftime("%d %b %Y") if parsed else "\u2014"


def show(value) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def event_class(action: str) -> str:
    if action.endswith(".create") or action.endswith(".register"):
        return "create"
    if action == "loan.checkout":
        return "checkout"
    if action.startswith("loan.") and "return" in action:
        return "ret"
    if action in {"item.archive", "account.disable", "request.decline"}:
        return "archive"
    if action.startswith("auth.") or action.startswith("account."):
        return "auth"
    return ""


def hold_class(state: str) -> str:
    """Pill colour for a unit's condition.

    'ok' is deliberately green rather than neutral: on the unit list most rows
    are fine, and the eye should land on the ones that are not.
    """
    return {
        "ok": "ok",
        "broken": "out",
        "repair": "warn",
        "missing": "warn",
        "gone": "grey",
    }.get(state, "grey")


def status_class(status: str) -> str:
    return {
        "pending": "warn", "approved": "info", "fulfilled": "ok",
        "declined": "out", "cancelled": "grey",
        "active": "ok", "disabled": "out",
    }.get(status, "grey")


templates.env.filters["datetime"] = fmt_datetime
templates.env.filters["date"] = fmt_date
templates.env.filters["show"] = show
templates.env.filters["event_class"] = event_class
templates.env.filters["status_class"] = status_class
templates.env.filters["hold_class"] = hold_class


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------
def _error_page(request: Request, code: int, title: str, message: str):
    """Render an error without needing a session (used before auth resolves)."""
    return templates.TemplateResponse(
        request, "error.html",
        {
            "code": code, "title": title, "message": message,
            "account": None, "actor": None, "csrf_token": "",
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
            "org": config.ORG_NAME, "path": request.url.path,
            "summary": service.summary(db.connect()),
            "pending_requests": 0, "pending_accounts": 0,
            "ok": "", "error": "",
            # base.html renders the sign-in link from these. An error page is
            # exactly where somebody is most likely to want it, and an
            # undefined variable would render href="".
            "auth_mode": config.AUTH_MODE,
            "sign_in_url": login_url(request.url.path),
        },
        status_code=code,
    )


@app.exception_handler(_LoginRedirect)
async def _needs_login(request: Request, exc: _LoginRedirect):
    return RedirectResponse(login_url(exc.next), status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return _error_page(request, 403, "Not permitted", exc.message)


@app.exception_handler(CSRFError)
async def _csrf_failed(request: Request, exc: CSRFError):
    return _error_page(request, 403, "Form expired", str(exc))


@app.exception_handler(NotFound)
async def _not_found(request: Request, exc: NotFound):
    return _error_page(request, 404, "Not found", str(exc))


@app.exception_handler(StockroomError)
async def _stockroom_error(request: Request, exc: StockroomError):
    """An expected, rejected operation: show it as a message, not a crash.

    The destination is taken from Referer, which is attacker-controlled, so it
    goes through safe_path -- otherwise this handler is an open redirect.

    Referer is an *absolute* URL, though, which safe_path refuses outright: it
    was written for a `next` form field. So this handler always fell back to
    "/" and quietly bounced people to the dashboard instead of back to the
    page they were on. Reduce a same-origin Referer to its path and query
    first, and let safe_path judge that.
    """
    return redirect(safe_path(_referer_path(request)), error=str(exc))


def _referer_path(request: Request) -> str:
    """The local path and query of a same-origin Referer, or "".

    Same-origin is decided against the Host this request arrived on, which
    HostCheckMiddleware has already confirmed is one we answer to -- so a
    forged Host cannot be used to make a foreign Referer look local here.
    """
    referer = request.headers.get("referer", "")
    if not referer:
        return ""
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc.lower() != request.headers.get("host", "").lower():
        return ""
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/api", "/static", "/public")):
        return await http_exception_handler(request, exc)
    return _error_page(request, exc.status_code, "Something went wrong", str(exc.detail))


# ---------------------------------------------------------------------------
# operational endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Liveness probe. Public, and deliberately says nothing sensitive.

    Counts the items directly rather than through service.summary(), which is
    six queries -- one of them a full COUNT(*) over the append-only event
    table -- and was being paid in full on every probe to obtain a single
    integer.
    """
    conn = db.connect()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "schema_version": db.get_meta(conn, "schema_version"),
            "item_count": conn.execute(
                "SELECT COUNT(*) AS n FROM item WHERE archived_at IS NULL"
            ).fetchone()["n"],
            # See publish/render.build_payload for why this is public.
            "audit_head": service.audit_head(conn),
        }
    )


@app.post("/publish")
def republish(request: Request):
    from .deps import require_staff

    require_staff(request)
    publish_worker.publish_now(config.DB_PATH)
    return redirect("/", ok="Public page rebuilt.")


# ---------------------------------------------------------------------------
# the generated public page
# ---------------------------------------------------------------------------
_PUBLIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


@app.get("/photos/{filename}", include_in_schema=False)
def item_photo(request: Request, filename: str):
    """Serve one stored item photo.

    A route rather than a StaticFiles mount, for the same two reasons /public
    is one: config.PHOTO_DIR is read per request rather than bound at import,
    and the path is resolved and checked against the root on every call.

    Requires a session. Photos are internal: the public page is a single
    self-contained file with no asset directory, and giving it one is a
    separate decision from being able to see what a cable looks like.
    """
    require_account(request)

    root = config.PHOTO_DIR.resolve()
    try:
        target = (root / filename).resolve()
    except (OSError, ValueError):
        raise StarletteHTTPException(status_code=404, detail="Not found")

    # The same guard as public_page: resolve both ends, then compare. A name
    # out of the database still goes through it, because a path built from a
    # stored string is exactly what becomes a traversal after a later change.
    if not target.is_relative_to(root) or not target.is_file():
        raise StarletteHTTPException(status_code=404, detail="Not found")

    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={
            # Filenames are random and content never changes once written, so
            # this is safe to cache hard. Removing a photo removes the row, and
            # nothing links to a filename that has no row.
            "Cache-Control": "private, max-age=604800",
        },
    )


@app.get("/public", include_in_schema=False)
def public_root_redirect():
    return RedirectResponse("/public/", status_code=307)


@app.get("/public/{path:path}", include_in_schema=False)
def public_page(path: str = ""):
    """Serve the generated public site.

    A route rather than a StaticFiles mount, because a mount binds its
    directory at import time while config.PUBLISH_DIR comes from the
    environment -- reading it per request keeps served and published in step.
    """
    root = config.PUBLISH_DIR.resolve()
    target = (root / (path or "index.html")).resolve()

    if not target.is_relative_to(root):
        raise StarletteHTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise StarletteHTTPException(
            status_code=404,
            detail="The public page has not been generated yet.",
        )
    return FileResponse(
        target,
        media_type=_PUBLIC_TYPES.get(target.suffix, "application/octet-stream"),
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# mounts
# ---------------------------------------------------------------------------
app.include_router(routes_auth.router)
app.include_router(routes_items.router)
app.include_router(routes_loans.router)
app.include_router(routes_people.router)
app.include_router(routes_history.router)
app.include_router(routes_requests.router)
app.include_router(routes_accounts.router)
app.include_router(routes_admin.router)
app.include_router(routes_counter.router)
app.include_router(routes_kits.router)
app.include_router(routes_stocktake.router)
# Registered whatever STOCKROOM_AUTH_MODE says. A route table that
# changed shape with the configuration would mean the tests that walk it
# only ever saw one shape of the application.
app.include_router(routes_sso.router)

app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")
