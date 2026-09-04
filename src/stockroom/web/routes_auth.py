"""Sign in, sign up, sign out, and managing your own account."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import accounts, config, security
from ..service import ConflictError, StockroomError, ValidationError
from .deps import (
    clear_session_cookie,
    client_ip,
    current_account,
    get_conn,
    login_url,
    page,
    redirect,
    require_account,
    safe_path,
    session_token,
    set_session_cookie,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if current_account(request) is not None:
        return redirect(safe_path(next))
    if config.AUTH_MODE == "sso":
        # There is no password form to show. Kept as a route rather than
        # deleted so that every bookmark, every link in the documentation and
        # deps.login_url's fallback all still land somewhere sensible.
        return redirect(f"/sso/login?next={quote(safe_path(next), safe='/')}")
    return page(request, "login.html", next_url=safe_path(next))


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Authenticate.

    Every failure -- unknown address, wrong password, unapproved account --
    produces the same message, from `accounts.login`. Distinguishing them
    would let anyone with a browser work out who has an account here.
    """
    target = safe_path(next)
    try:
        result = accounts.login(
            get_conn(),
            email=email,
            password=password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    except accounts.AuthError as exc:
        # login_url encodes the destination: a `next` carrying its own query
        # string would otherwise put its parameters onto /login, where an
        # `ok=` is rendered as a flash message on the sign-in page.
        return redirect(login_url(target), error=str(exc))

    response = redirect(target, ok=f"Signed in as {result.account.name}.")
    # A brand-new token, so a session fixed before login cannot be reused.
    set_session_cookie(response, request, result.token)
    return response


@router.post("/logout")
def logout(request: Request):
    token = session_token(request)
    if token:
        accounts.logout(get_conn(), token=token)
    # Under single sign-on, "/login" would forward to RIT, RIT would still
    # recognise the browser, and the person would be signed straight back in
    # -- so Sign out would appear to do nothing. See sso_signed_out.html.
    target = "/sso/signed-out" if config.AUTH_MODE == "sso" else "/login"
    response = redirect(target, ok="Signed out.")
    clear_session_cookie(response, request)
    return response


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if current_account(request) is not None:
        return redirect("/")
    return page(request, "register.html", min_length=security.MIN_PASSWORD_LENGTH)


@router.post("/register")
def register(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    """Create a pending account.

    The response is identical whether or not the address was already
    registered. Saying "that email is taken" would turn this form into a
    lookup tool for who works here.
    """
    if password != password_confirm:
        return redirect("/register", error="The two passwords do not match.")

    submitted = "Thanks. Your account is awaiting approval by stockroom staff — "
    submitted += "you will be able to sign in once it is approved."

    try:
        accounts.register(
            get_conn(),
            first_name=first_name,
            last_name=last_name,
            email=email,
            password=password,
        )
    except ConflictError:
        # Already registered. Report success anyway -- see the docstring.
        return redirect("/login", ok=submitted)
    except (ValidationError, security.PasswordError) as exc:
        # These are about what the user typed, not about who exists, so they
        # are safe (and necessary) to show.
        return redirect("/register", error=str(exc))
    except StockroomError as exc:
        return redirect("/register", error=str(exc))

    return redirect("/login", ok=submitted)


@router.get("/account", response_class=HTMLResponse)
def my_account(request: Request):
    account = require_account(request)
    conn = get_conn()
    from .. import requests_service, service

    loans = []
    if account.person_id is not None:
        loans = service.list_loans(conn, person_id=account.person_id, open_only=True)

    from .. import db

    return page(
        request,
        "account.html",
        subject=account,
        sessions=accounts.list_sessions(conn, account.id),
        my_requests=requests_service.list_requests(conn, requester_id=account.id, limit=20),
        my_loans=loans,
        now=db.utcnow(),
        min_length=security.MIN_PASSWORD_LENGTH,
        next_url="/account",
    )


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
):
    account = require_account(request)
    if new_password != new_password_confirm:
        return redirect("/account", error="The two new passwords do not match.")
    try:
        accounts.change_password(
            get_conn(),
            actor=account.as_actor(),
            account_id=account.id,
            new_password=new_password,
            current_password=current_password,
        )
    except (accounts.AuthError, security.PasswordError, StockroomError) as exc:
        return redirect("/account", error=str(exc))

    # change_password revoked every session, including this one.
    response = redirect(
        "/login", ok="Password changed. Please sign in again."
    )
    clear_session_cookie(response, request)
    return response


@router.post("/account/revoke-sessions")
def revoke_sessions(request: Request):
    account = require_account(request)
    accounts.revoke_all_sessions(
        get_conn(), actor=account.as_actor(), account_id=account.id
    )
    response = redirect("/login", ok="Signed out on every device.")
    clear_session_cookie(response, request)
    return response
