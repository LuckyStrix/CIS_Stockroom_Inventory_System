"""Single sign-on against RIT's Shibboleth identity provider.

Four routes, all public, because every one of them happens before there is
anyone to be signed in as:

    GET  /sso/login       start: build an AuthnRequest, send the browser to RIT
    POST /sso/acs         finish: validate what RIT sent back, open a session
                          (answers with a page, not a redirect -- see below)
    GET  /sso/metadata    our own metadata, which ITS need to register us
    GET  /sso/signed-out  where signing out lands, and why it says what it says

The protocol lives in `stockroom/saml.py`; the handshake state lives in
`stockroom/security.py`. What is left here is HTTP.

The thing worth understanding before changing anything in this file is that
**single sign-on does not replace the identity seam, it feeds it.** /sso/acs
finishes by calling `accounts.sso_login`, which creates an ordinary row in the
`session` table with an ordinary token in an ordinary cookie. `current_account`
is untouched, and so is every route, every service module and the audit log.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from .. import accounts, config, saml, security
from ..service import StockroomError
from .deps import (
    clear_saml_state_cookie,
    client_ip,
    current_account,
    get_conn,
    page,
    redirect,
    require_saml_handshake,
    safe_path,
    set_saml_state_cookie,
    set_session_cookie,
    templates,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# A started sign-in writes a database row, and this is a GET that anyone on
# the campus network can call. One row per click is fine; ten thousand is a
# way to fill an SD card. In memory rather than the database for the same
# reason the lockout log throttle is: losing it on restart costs nothing.
_start_throttle = security.RateLimiter(limit=30, per_seconds=60)

# /sso/metadata is public and unauthenticated, and answering it is not free:
# it parses RIT's metadata, assembles our settings, builds a document and
# validates it. The parse is cached (saml._material), the rest is not. ITS
# fetch this occasionally and a person runs `stockroom sso metadata` by hand;
# nothing legitimate needs it in a loop.
_metadata_throttle = security.RateLimiter(limit=10, per_seconds=60)


def _with_flash(path: str, message: str) -> str:
    """The `?ok=` that `redirect()` would have added, for a page that cannot.

    Kept identical to deps.redirect's construction on purpose: the landing
    page hands the browser a URL, and the flash has to survive that hop the
    same way it survives a redirect.
    """
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}ok={quote(message)}"


def _unavailable() -> str:
    """Why single sign-on cannot be used right now, or "" if it can."""
    if config.AUTH_MODE == "password":
        return "Single sign-on is not enabled on this server."
    problems = saml.missing_pieces()
    if problems:
        # Said plainly on purpose. This is only reachable when an
        # administrator has turned SSO on, and "it doesn't work" with no
        # detail is how an afternoon gets lost.
        logger.error("SSO is enabled but not usable: %s", "; ".join(problems))
        return "Single sign-on is not finished being set up on this server."
    return ""


@router.get("/sso/login")
def start(request: Request, next: str = "/"):
    """Send the browser to RIT, remembering what we asked and who asked it."""
    if current_account(request) is not None:
        return redirect(safe_path(next))

    unavailable = _unavailable()
    if unavailable:
        return redirect("/login", error=unavailable)

    if not _start_throttle.allow(f"sso:{client_ip(request)}"):
        return redirect("/login", error="Too many sign-in attempts. Try again shortly.")

    # Two separate secrets. `state` goes to the browser and never to RIT;
    # `relay` goes to RIT and comes back verbatim. Neither is the destination:
    # `next` stays on the server, so no caller-supplied redirect target ever
    # travels through another organisation's server -- or its logs.
    state = security.new_token()
    relay = security.new_token()

    try:
        authn = saml.build_authn_request(relay_state=relay)
    except saml.SamlError as exc:
        logger.error("SSO: could not build an authentication request: %s", exc)
        return redirect("/login", error="Single sign-on is unavailable right now.")

    security.open_saml_handshake(
        get_conn(),
        request_id=authn.request_id,
        state_token=state,
        relay_state=relay,
        return_to=safe_path(next),
        ip=client_ip(request),
    )

    response = redirect(authn.redirect_url)
    set_saml_state_cookie(response, request, state)
    return response


@router.post("/sso/acs")
def assertion_consumer(
    request: Request,
    SAMLResponse: str = Form(""),
    RelayState: str = Form(""),
):
    """Finish a sign-in RIT has vouched for.

    This is the only POST in the application that does not carry a CSRF token
    -- it is a cross-site form POST from an identity provider that has never
    seen one. `require_saml_handshake` is what replaces it, and it is the
    first statement here for that reason.

    Note `Form("")` rather than `Form(...)`: a required field would make a
    junk POST a 422, and this route answers 403 to everything it refuses, like
    every other POST in the application.
    """
    signin = require_saml_handshake(
        request, saml_response=SAMLResponse, relay_state=RelayState
    )

    assertion = signin.assertion
    try:
        result = accounts.sso_login(
            get_conn(),
            sso_uid=assertion.sso_uid,
            email=assertion.email,
            first_name=assertion.first_name,
            last_name=assertion.last_name,
            affiliation=assertion.affiliation,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    except accounts.AuthError as exc:
        # RIT proved who they are; the stockroom is not letting them in. That
        # is a different thing from a failed password and is safe to say --
        # there is nothing to enumerate, because they already know who they
        # are.
        response = redirect("/login", error=str(exc))
        clear_saml_state_cookie(response, request)
        return response
    except StockroomError as exc:
        logger.warning("SSO: sign-in refused for %s: %s", assertion.email, exc)
        response = redirect("/login", error=str(exc))
        clear_saml_state_cookie(response, request)
        return response

    # A rendered page rather than a redirect, and this is the one thing in
    # the SSO flow that is NOT obvious.
    #
    # The session cookie is SameSite=Strict. A Strict cookie is withheld from
    # any navigation a cross-site page initiated -- and that includes every
    # hop of a redirect chain that began cross-site, which is exactly what we
    # are in the middle of: RIT's browser-side form POSTed here. So a 303 to
    # `return_to` arrives WITHOUT the cookie we just set. Under
    # AUTH_MODE="sso" that is a loop, because landing signed-out sends the
    # browser back to /sso/login, RIT still recognises it, and round it goes;
    # under "both" it merely dumps the person on the password form having
    # just signed in.
    #
    # Serving a page on our own origin ends the cross-site chain. The meta
    # refresh in it is a navigation THIS page initiates, so it is same-site,
    # so the Strict cookie goes with it. Setting the cookie here is fine --
    # SameSite governs when a cookie is sent, not when it may be set.
    #
    # The alternative was to make the session cookie Lax, which would have
    # worked and would have loosened a property the whole application relies
    # on, in every mode, for the sake of one route. Covered by
    # test_the_landing_page_is_what_carries_a_strict_cookie_home.
    target = _with_flash(signin.return_to, f"Signed in as {result.account.name}.")
    response = templates.TemplateResponse(
        request, "sso_landing.html", {"target": target}
    )
    set_session_cookie(response, request, result.token)
    clear_saml_state_cookie(response, request)
    return response


@router.get("/sso/metadata")
def metadata(request: Request):
    """The document RIT ITS need in order to register this service provider.

    Public because it has to be: ITS fetch it, and it contains nothing secret
    -- an entityID, a URL and a public key. Public and cheap are different
    questions, though, hence the throttle.
    """
    if not _metadata_throttle.allow(f"sso-metadata:{client_ip(request)}"):
        return Response(
            content="Too many requests for the metadata. Try again shortly.\n",
            media_type="text/plain",
            status_code=429,
        )
    try:
        return Response(content=saml.sp_metadata(), media_type="application/xml")
    except saml.SamlError as exc:
        # 503 rather than 500: this is "not set up yet", which is a state the
        # Pi is legitimately in for as long as the ITS ticket takes.
        return Response(
            content=f"Single sign-on is not configured: {exc}\n",
            media_type="text/plain",
            status_code=503,
        )


@router.get("/sso/signed-out", response_class=HTMLResponse)
def signed_out(request: Request):
    """Where signing out lands when RIT is the identity provider.

    It exists because /login would be wrong. Under AUTH_MODE=sso, /login
    forwards to the identity provider, RIT still has a live session, and the
    person would be signed straight back in -- so "Sign out" would visibly do
    nothing at all. This page is a full stop, and it tells the truth about
    what has and has not ended.
    """
    return page(request, "sso_signed_out.html")
