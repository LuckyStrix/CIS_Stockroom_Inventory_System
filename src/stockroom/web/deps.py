"""Request plumbing: identity, authorisation, CSRF, templates, flashes.

    ======================================================================
    THE IDENTITY SEAM: `current_account()` is the one function that decides
    who is making a request. Routes take an Account (or an Actor) and never
    ask where it came from, so replacing session cookies with Shibboleth is
    a change to this file alone -- see docs/sso-integration.md.
    ======================================================================

Phase 1 read a self-declared name from a cookie. That is gone: identity now
comes from a server-side session, and everything that changes data requires
one.

Authorisation is expressed as dependencies -- `require_staff`, `require_admin`
-- so a route's permissions are visible in its signature rather than buried in
its body. `tests/test_authz.py` walks every route in the application and fails
if one is neither explicitly public nor protected, which is what stops a new
route being added unguarded by accident.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import accounts, config, db, saml, security, service
from ..accounts import Account
from ..service import Actor

_PACKAGE_DIR = Path(__file__).resolve().parent.parent

templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))

# The __Host- prefix is enforced by the browser: it refuses the cookie unless
# it is Secure, Path=/ and has no Domain. That makes it impossible for a
# subdomain -- or a network attacker who can spoof one -- to plant a session
# cookie for this site. It requires HTTPS, so plain-HTTP development falls
# back to the unprefixed name.
SESSION_COOKIE_SECURE = "__Host-stockroom_session"
SESSION_COOKIE_PLAIN = "stockroom_session"

CSRF_FIELD = "_csrf"

# Anonymous visitors need CSRF protection too -- the login form is a real
# target. An attacker who can make your browser POST to /login with *their*
# credentials logs you into their account, and then watches what you do in it.
# So there are two token sources:
#
#   signed in  -> the token stored on the session row (synchroniser pattern)
#   anonymous  -> a token in its own cookie, echoed in the form (double submit)
#
# The session-bound token is the stronger of the two and is used whenever one
# exists; the cookie only covers the handful of pre-login forms.
CSRF_COOKIE = "stockroom_csrf"

# Routes reachable without a session. Everything else requires one; the test
# that enumerates routes uses this same list, so it cannot drift from reality.
PUBLIC_PATHS = frozenset({
    "/login", "/logout", "/register", "/health",
    # The bare path is its own route: a 307 to /public/. It is listed here
    # rather than as a prefix because a bare "/public" prefix also matches
    # "/public-holidays" and "/publicfoo", quietly exempting any future route
    # whose path merely starts with those seven characters.
    "/public",
    "/openapi.json", "/api/docs", "/redoc", "/docs/oauth2-redirect",
    # Single sign-on. Listed one by one rather than as a "/sso/" prefix for
    # the same reason "/public" is: a prefix would also exempt any future
    # route whose path merely starts with those characters.
    "/sso/login", "/sso/acs", "/sso/metadata", "/sso/signed-out",
})
PUBLIC_PREFIXES = ("/static/", "/public/")

# The one path whose CSRF token is REPLACED by something stronger, not simply
# dropped. /sso/acs is a top-level cross-site form POST from RIT's identity
# provider, which has never seen our token and cannot send one.
#
# What stands in for it is in security.consume_saml_handshake: a signed
# assertion whose InResponseTo names a sign-in this server started, bound by a
# cookie to the browser that started it, single-use, and valid for five
# minutes. That is stronger than the token it replaces -- an attacker can put
# any value in a form field, but cannot forge RIT's signature.
#
# Nothing else belongs in here. test_the_csrf_exemption_is_exactly_one_path
# fails if the set grows, so adding to it is a decision somebody has to make
# on purpose.
CSRF_EXEMPT_PATHS = frozenset({"/sso/acs"})


def is_csrf_exempt(path: str) -> bool:
    return path in CSRF_EXEMPT_PATHS


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def get_conn() -> sqlite3.Connection:
    return db.connect()


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------


def cookie_name(request: Request) -> str:
    return SESSION_COOKIE_SECURE if _is_secure(request) else SESSION_COOKIE_PLAIN


def _is_secure(request: Request) -> bool:
    """Whether this request arrived over TLS.

    In production nginx terminates TLS and forwards over loopback, so the
    scheme comes from X-Forwarded-Proto -- which uvicorn only honours with
    --proxy-headers, and which is only trustworthy because nothing but nginx
    can reach the app's port.
    """
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def set_session_cookie(response, request: Request, token: str) -> None:
    secure = _is_secure(request)
    response.set_cookie(
        cookie_name(request),
        token,
        max_age=accounts.ABSOLUTE_TIMEOUT_DAYS * 24 * 3600,
        httponly=True,      # JavaScript can never read it, so XSS cannot steal it
        secure=secure,
        samesite="strict",  # defence in depth behind the CSRF token
        path="/",
    )


def clear_session_cookie(response, request: Request) -> None:
    for name in (SESSION_COOKIE_SECURE, SESSION_COOKIE_PLAIN):
        response.delete_cookie(name, path="/")
    # Drop the anonymous token too, so the next sign-in starts clean.
    response.delete_cookie(CSRF_COOKIE, path="/")


# The browser half of what replaces the CSRF token on /sso/acs. A nonce goes
# out in this cookie and its hash is stored on the handshake row; only a
# browser that shows the nonce can spend the handshake.
SAML_STATE_COOKIE_SECURE = "__Host-stockroom_saml"
SAML_STATE_COOKIE_PLAIN = "stockroom_saml"


def saml_cookie_name(request: Request) -> str:
    return (
        SAML_STATE_COOKIE_SECURE if _is_secure(request) else SAML_STATE_COOKIE_PLAIN
    )


def set_saml_state_cookie(response, request: Request, token: str) -> None:
    """Remember, in this browser, that this browser started this sign-in.

    SameSite=None, and that is not a loosening -- it is the only value that
    works. The identity provider replies with a top-level cross-site POST,
    and a Lax cookie is only sent on a cross-site request that is a
    *navigation with a safe method*. Lax here would mean the cookie never
    arrives and every single sign-in fails. Strict is worse still.

    None requires Secure, so single sign-on requires TLS. That is fine in the
    stockroom, which is https-only, and it is why plain HTTP falls back to
    Lax and the unprefixed name -- enough for the test client, and for
    nothing else.

    What bounds the risk of a cross-site-readable cookie is that it is worth
    almost nothing: it is not the session, it is one-time, it expires in
    minutes, and the only thing it can do is finish one specific pending
    sign-in that also needs a signed assertion from RIT.
    """
    secure = _is_secure(request)
    response.set_cookie(
        saml_cookie_name(request),
        token,
        max_age=config.SSO_HANDSHAKE_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="none" if secure else "lax",
        path="/",
    )


def saml_state_token(request: Request) -> str:
    return (
        request.cookies.get(SAML_STATE_COOKIE_SECURE)
        or request.cookies.get(SAML_STATE_COOKIE_PLAIN)
        or ""
    )


def clear_saml_state_cookie(response, request: Request) -> None:
    for name in (SAML_STATE_COOKIE_SECURE, SAML_STATE_COOKIE_PLAIN):
        response.delete_cookie(name, path="/")


def session_token(request: Request) -> str:
    return (
        request.cookies.get(SESSION_COOKIE_SECURE)
        or request.cookies.get(SESSION_COOKIE_PLAIN)
        or ""
    )


def client_ip(request: Request) -> str:
    """The caller's address, preferring nginx's X-Forwarded-For.

    Only meaningful because the app is not directly reachable: nothing but the
    local reverse proxy can set that header here.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def current_session(request: Request):
    """Resolve the session for this request, caching it on request.state.

    Cached because a single page render may consult identity several times
    (navigation, permissions, CSRF) and each call would otherwise be a lookup
    plus an idle-expiry write.
    """
    if hasattr(request.state, "session_pair"):
        return request.state.session_pair
    pair = accounts.resolve_session(get_conn(), session_token(request))
    request.state.session_pair = pair
    return pair


def current_account(request: Request) -> Account | None:
    """Who is making this request, or None if nobody is signed in."""
    pair = current_session(request)
    return pair[1] if pair else None


def current_actor(request: Request) -> Actor | None:
    """The audit-log identity for this request."""
    account = current_account(request)
    return account.as_actor() if account else None


def csrf_token(request: Request) -> str:
    """The CSRF token this request's forms should carry.

    Prefers the session-bound token. Falls back to the anonymous cookie,
    minting one (and remembering to set it) if the browser has none yet.
    """
    pair = current_session(request)
    if pair:
        return pair[0].csrf_token

    existing = request.cookies.get(CSRF_COOKIE, "")
    if existing:
        return existing

    minted = getattr(request.state, "new_csrf_cookie", "")
    if not minted:
        minted = security.new_token()
        # Picked up by the security middleware, which sets it on the response.
        request.state.new_csrf_cookie = minted
    return minted


def expected_csrf(request: Request) -> str:
    """The token a submission must match, without minting a new one.

    Deliberately does *not* fall back to a freshly minted value: a POST that
    arrives with no session and no cookie has nothing to check against and
    must fail, not be handed a token that matches whatever it sent.
    """
    pair = current_session(request)
    if pair:
        return pair[0].csrf_token
    return request.cookies.get(CSRF_COOKIE, "")


def set_csrf_cookie(response, request: Request) -> None:
    """Persist a freshly minted anonymous CSRF token, if one was needed."""
    minted = getattr(request.state, "new_csrf_cookie", "")
    if not minted:
        return
    response.set_cookie(
        CSRF_COOKIE,
        minted,
        max_age=8 * 3600,
        httponly=True,     # the server echoes it into the form; JS never needs it
        secure=_is_secure(request),
        samesite="lax",    # must survive a normal top-level navigation to /login
        path="/",
    )


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------


class _LoginRedirect(Exception):
    """Raised when an anonymous caller reaches a protected page."""

    def __init__(self, request: Request) -> None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        self.next = target
        super().__init__("authentication required")


class Forbidden(Exception):
    """Signed in, but not permitted to do this."""

    def __init__(self, message: str = "You do not have permission to do that.") -> None:
        self.message = message
        super().__init__(message)


def require_account(request: Request) -> Account:
    account = current_account(request)
    if account is None:
        raise _LoginRedirect(request)
    return account


def require_staff(request: Request) -> Account:
    account = require_account(request)
    if not account.is_staff:
        raise Forbidden("That action is limited to stockroom staff.")
    return account


def require_admin(request: Request) -> Account:
    account = require_account(request)
    if not account.is_admin:
        raise Forbidden("That action is limited to administrators.")
    return account


@dataclass(frozen=True, slots=True)
class SsoSignIn:
    """A validated assertion, and where the person was trying to go."""

    assertion: "saml.Assertion"
    return_to: str


def require_saml_handshake(
    request: Request, *, saml_response: str, relay_state: str
) -> SsoSignIn:
    """Everything /sso/acs must be sure of before it does anything at all.

    This is the control that stands in for the CSRF token on that route (see
    CSRF_EXEMPT_PATHS). It is named `require_*` deliberately: the AST check in
    test_no_route_does_work_before_its_permission_check finds any call whose
    name starts with that and insists it be the handler's first statement, so
    the compensating control is held in first position by an invariant that
    already existed, rather than by a comment asking nicely.

    Order is deliberate. The browser binding is checked and spent first, then
    the signature. Nothing parses attacker-supplied XML until we know this
    browser asked for it.

    Every failure is the same 403. The real reason goes to the log, where an
    administrator can see it and a prober cannot.
    """
    logger = logging.getLogger(__name__)

    if not saml_response:
        logger.warning("SSO: a POST reached /sso/acs with no SAMLResponse")
        raise Forbidden("That sign-in could not be completed. Please try again.")

    conn = get_conn()
    try:
        handshake = security.consume_saml_handshake(
            conn, state_token=saml_state_token(request), relay_state=relay_state
        )
    except security.HandshakeError as exc:
        logger.warning("SSO: handshake refused: %s", exc)
        raise Forbidden("That sign-in could not be completed. Please try again.")

    try:
        assertion = saml.parse_response(
            saml_response=saml_response, request_id=handshake.request_id
        )
    except saml.SamlError as exc:
        logger.warning("SSO: assertion refused: %s", exc)
        raise Forbidden("That sign-in could not be completed. Please try again.")

    return SsoSignIn(assertion=assertion, return_to=safe_path(handshake.return_to))


# FastAPI dependency forms, so a route's permissions show up in its signature.
CurrentAccount = Depends(require_account)
StaffOnly = Depends(require_staff)
AdminOnly = Depends(require_admin)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


class CSRFError(Exception):
    """A state-changing request arrived without a valid token."""


def verify_csrf(request: Request, submitted: str) -> None:
    """Check a synchroniser token against the one held in the session.

    SameSite=Strict already blocks the common cross-site POST, but it is a
    browser behaviour, not a guarantee -- older clients and odd embeddings do
    not honour it. The token is the actual control; the cookie attribute is
    the belt to its braces.
    """
    expected = expected_csrf(request)
    if not expected or not security.tokens_equal(expected, submitted or ""):
        raise CSRFError(
            "That form has expired or was not submitted from this site. "
            "Reload the page and try again."
        )


async def csrf_protect(request: Request) -> None:
    """Dependency that guards every unsafe method.

    Applied globally by middleware in app.py rather than route by route --
    remembering to add it to each new POST is exactly the kind of thing that
    gets forgotten.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    form = await request.form()
    verify_csrf(request, str(form.get(CSRF_FIELD, "")))


# ---------------------------------------------------------------------------
# redirects and flash messages
# ---------------------------------------------------------------------------


def safe_path(candidate: str, fallback: str = "/") -> str:
    """Reduce a caller-supplied redirect target to a local path.

    Used for every redirect whose destination came from outside -- a `next`
    parameter or a Referer header. Without it those are open redirects, which
    turn this site into a convincing launch pad for a phishing link.
    """
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback              # absolute URL: refuse it
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback              # relative or protocol-relative: refuse it
    # A backslash is a path separator to a browser resolving a URL, so
    # `/\evil.example` is read as `//evil.example` -- the protocol-relative
    # form refused one line above, wearing a different hat. Starlette happens
    # to percent-encode it on the way out, which makes this unexploitable
    # today, but that is somebody else's implementation detail holding the
    # line rather than this function doing its job.
    if "\\" in candidate:
        return fallback
    return candidate


def local_to_utc(value: str, *, end: bool = False) -> str | None:
    """Read a date or datetime typed into a form, in the stockroom's timezone.

    `<input type="datetime-local">` yields "2026-09-03T14:00" and
    `<input type="date">` yields "2026-09-03". Both are wall-clock times in
    the building, and everything here is stored in UTC, so they are converted
    rather than having a Z stapled on -- which is what used to happen, and it
    made a loan due "Friday" go overdue at 19:59 on Friday afternoon, four
    hours before the day it was entered for had ended.

    A bare date used as a deadline becomes the end of that local day, so
    something due Friday is not overdue at midnight on Friday morning.

    Lived in routes_requests as _as_utc, alongside two hand-rolled copies of
    the end-of-day line in routes_counter and routes_loans. That duplication
    is why the same bug had to be fixed in three places.
    """
    value = (value or "").strip()
    if not value:
        return None

    if len(value) == 10:                    # a date with no time
        stamp = f"{value}T23:59:59" if end else f"{value}T00:00:00"
    elif len(value) == 16:                  # datetime-local, no seconds
        stamp = f"{value}:00"
    elif value.endswith("Z"):
        return value                        # already UTC: pass it through
    else:
        stamp = value

    try:
        naive = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        # Not a shape we recognise. Returning it unchanged is what this did
        # before, and the service layer is where a bad value gets rejected.
        return value
    return (
        naive.replace(tzinfo=config.TIMEZONE)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def utc_to_local(stamp: str | None) -> datetime | None:
    """Parse a stored UTC timestamp into the stockroom's local time.

    The display half of local_to_utc. Returns None for anything unparseable,
    so a template filter can print an em dash rather than raising.
    """
    if not stamp:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            parsed = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).astimezone(config.TIMEZONE)
    return None


def login_url(target: str) -> str:
    """The sign-in URL that comes back here afterwards.

    `target` is percent-encoded, and that is the whole point of this function
    existing rather than three f-strings. A destination carries its own query
    string -- /items?unit=B&filter=out -- and interpolating it raw let its
    parameters land on /login as *login's* parameters. Two consequences:

      * the destination came back truncated at the first `&`;
      * deps.page() renders `?ok=` and `?error=` as flash messages, so
        anyone could put arbitrary text on the real sign-in page by sending
        a link to /anything?x=1&ok=Your+password+has+expired.+Call+... It is
        escaped, so not script -- but a phishing message in the site's own
        voice, on the genuine page, over the genuine certificate.

    Encoding here means the destination survives intact and cannot smuggle a
    parameter, whatever it contains.

    Under STOCKROOM_AUTH_MODE=sso this points at the identity provider hop
    instead. Keeping that decision here means the deny-by-default gate in
    app.require_authentication, the _LoginRedirect handler and every template
    follow it without knowing about it -- there is one seam, not four. The
    mode is read per call, never bound at import, so a restart is all it takes
    to change it and a test can monkeypatch it.
    """
    encoded = quote(safe_path(target), safe="/")
    if config.AUTH_MODE == "sso":
        return f"/sso/login?next={encoded}"
    return f"/login?next={encoded}"


def redirect(path: str, *, ok: str = "", error: str = "") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{separator}ok={quote(ok)}"
    elif error:
        path = f"{path}{separator}error={quote(error)}"
    return RedirectResponse(path, status_code=303)


def page(request: Request, template: str, **context):
    """Render a template with the context every page needs."""
    conn = get_conn()
    account = current_account(request)

    context.setdefault("ok", request.query_params.get("ok", ""))
    context.setdefault("error", request.query_params.get("error", ""))

    # Badge counts for the navigation, but only for the people who can act on
    # them -- a requester has no inbox to clear.
    pending_requests = pending_accounts = 0
    if account is not None and account.is_staff:
        from .. import requests_service

        pending_requests = requests_service.count_pending(conn)
        pending_accounts = accounts.count_pending(conn)

    return templates.TemplateResponse(
        request,
        template,
        {
            "account": account,
            "actor": current_actor(request),
            "csrf_token": csrf_token(request),
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
            "org": config.ORG_NAME,
            "path": request.url.path,
            "summary": service.summary(conn),
            "pending_requests": pending_requests,
            "pending_accounts": pending_accounts,
            "auth_mode": config.AUTH_MODE,
            "sign_in_url": login_url(request.url.path),
            **context,
        },
    )
