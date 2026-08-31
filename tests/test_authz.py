"""Authentication and authorisation, enforced across every route.

The two enumerating tests here are the point of this file. They walk the
application's own route table, so adding a route without an authentication
decision, or a POST without CSRF protection, fails the build rather than
quietly shipping.
"""

import re

import pytest
from fastapi.testclient import TestClient

from stockroom import accounts, service
from stockroom.service import Actor
from stockroom.web import deps

SETUP = Actor("cli:test")
STRONG = "glass onion tuesday lamp"
OTHER = "seventeen purple bicycles"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(temp_env):
    from stockroom.web.app import app as fastapi_app

    return fastapi_app


@pytest.fixture
def admin(conn):
    return accounts.register(
        conn, first_name="Carter", last_name="Laubach", email="carter@rit.edu",
        password=STRONG, role="admin", status="active", actor=SETUP,
    )


@pytest.fixture
def requester(conn, admin):
    account = accounts.register(
        conn, first_name="Alice", last_name="Nguyen", email="an1234@rit.edu",
        password=OTHER, actor=SETUP,
    )
    return accounts.approve(
        conn, actor=admin.as_actor(), account_id=account.id, approved_by=admin
    )


def csrf(client: TestClient, path: str = "/login") -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text)
    return match.group(1) if match else ""


def sign_in(app, email: str, password: str) -> TestClient:
    client = TestClient(app)
    client.__enter__()
    token = csrf(client, "/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "next": "/", "_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    assert "stockroom_session" in client.cookies
    return client


@pytest.fixture
def staff_client(app, admin):
    client = sign_in(app, "carter@rit.edu", STRONG)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def requester_client(app, requester):
    client = sign_in(app, "an1234@rit.edu", OTHER)
    yield client
    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# the enumerating guards
# ---------------------------------------------------------------------------


def _app_routes(app):
    """Every (method, path) the application serves, flattened."""
    found = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            for sub in getattr(route, "routes", []) or []:
                for method in getattr(sub, "methods", set()) or set():
                    found.append((method, sub.path))
        else:
            for method in getattr(route, "methods", set()) or set():
                found.append((method, path))
    return found


def _concrete(path: str) -> str:
    """Fill path parameters with a plausible id so the route can be called."""
    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_route_is_either_public_or_requires_a_login(app, temp_env):
    """No route may be reachable anonymously unless it is deliberately public.

    Deny-by-default is enforced in middleware; this proves it holds for every
    route that actually exists, so a new page cannot be exposed by omission.
    """
    exposed = []
    with TestClient(app) as anonymous:
        for method, path in _app_routes(app):
            if method not in ("GET", "POST"):
                continue
            if deps.is_public_path(path) or path.startswith("/public"):
                continue
            response = anonymous.request(
                method, _concrete(path), follow_redirects=False
            )
            # Anonymous callers must be redirected to login (GET) or refused.
            # 403 also covers the CSRF gate, which fires before authentication.
            if response.status_code not in (303, 401, 403, 307):
                exposed.append(f"{method} {path} -> {response.status_code}")
    assert not exposed, "these routes are reachable without signing in: " + str(exposed)


def test_every_post_route_rejects_a_missing_csrf_token(app, requester_client):
    """A signed-in session is not enough; the token must be present."""
    unprotected = []
    for method, path in _app_routes(app):
        if method != "POST":
            continue
        response = requester_client.post(
            _concrete(path), data={"nothing": "here"}, follow_redirects=False
        )
        if response.status_code != 403:
            unprotected.append(f"POST {path} -> {response.status_code}")
    assert not unprotected, "these POST routes accepted a request with no CSRF token: " + str(unprotected)


def test_a_wrong_csrf_token_is_rejected(requester_client):
    response = requester_client.post(
        "/requests/new/new_item",
        data={"proposed_name": "X", "_csrf": "not-the-right-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_csrf_middleware_does_not_eat_the_request_body(requester_client, conn):
    """Regression: reading the body for CSRF must leave it readable downstream.

    Starlette only replays a request body to the route when middleware read it
    with `body()`. An earlier version called `form()` here, and every POST in
    the application failed validation with "field required".
    """
    token = csrf(requester_client, "/requests/new/new_item")
    response = requester_client.post(
        "/requests/new/new_item",
        data={"proposed_name": "A Second Tripod", "proposed_quantity": 2,
              "note": "the body must survive", "_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    from stockroom import requests_service

    filed = requests_service.list_requests(conn)
    assert filed and filed[0].proposed_name == "A Second Tripod"
    assert filed[0].proposed_quantity == 2


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/accounts", "/history", "/people", "/loans", "/requests",
             "/labels", "/export.csv"],
)
def test_a_requester_cannot_reach_staff_pages(requester_client, path):
    assert requester_client.get(path, follow_redirects=False).status_code == 403


@pytest.mark.parametrize(
    "path", ["/accounts", "/history", "/people", "/loans", "/requests",
             "/labels", "/export.csv"],
)
def test_staff_can_reach_staff_pages(staff_client, path):
    assert staff_client.get(path, follow_redirects=False).status_code == 200


@pytest.mark.parametrize("path", ["/", "/items", "/requests/mine", "/account"])
def test_a_requester_can_reach_their_own_pages(requester_client, path):
    assert requester_client.get(path, follow_redirects=False).status_code == 200


def test_a_requester_cannot_change_inventory(requester_client, conn, admin):
    item = service.create_item(conn, actor=admin.as_actor(), name="Camera", quantity=2)
    token = csrf(requester_client, "/")
    response = requester_client.post(
        f"/items/{item.id}/archive", data={"_csrf": token}, follow_redirects=False
    )
    assert response.status_code == 403
    assert not service.get_item(conn, item.id).is_archived


def test_only_an_admin_can_change_roles(staff_client, requester_client, conn, requester):
    """Staff can approve accounts; granting privilege is reserved for admins."""
    token = csrf(requester_client, "/")
    assert requester_client.post(
        f"/accounts/{requester.id}/role", data={"role": "admin", "_csrf": token},
        follow_redirects=False,
    ).status_code == 403
    assert accounts.get_account(conn, requester.id).role == "requester"

    token = csrf(staff_client, "/accounts")
    assert staff_client.post(
        f"/accounts/{requester.id}/role", data={"role": "staff", "_csrf": token},
        follow_redirects=False,
    ).status_code == 303
    assert accounts.get_account(conn, requester.id).role == "staff"


def test_a_requester_cannot_read_someone_elses_request(
    requester_client, staff_client, conn, admin, requester
):
    from stockroom import requests_service

    other = accounts.register(
        conn, first_name="Bob", last_name="Other", email="bo9876@rit.edu",
        password="Rochester-Fog-Kettle-9", actor=SETUP,
    )
    other = accounts.approve(conn, actor=admin.as_actor(), account_id=other.id,
                             approved_by=admin)
    theirs = requests_service.submit_new_item(
        conn, actor=other.as_actor(), requester_id=other.id, name="Private idea"
    )
    assert requester_client.get(f"/requests/{theirs.id}",
                                follow_redirects=False).status_code == 403
    # Staff can, because someone has to action it.
    assert staff_client.get(f"/requests/{theirs.id}").status_code == 200


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_login_is_refused_before_approval(app, conn, admin):
    accounts.register(conn, first_name="Pending", last_name="Person",
                      email="pp1234@rit.edu", password=OTHER, actor=SETUP)
    with TestClient(app) as client:
        token = csrf(client, "/login")
        response = client.post(
            "/login",
            data={"email": "pp1234@rit.edu", "password": OTHER, "_csrf": token},
            follow_redirects=False,
        )
        assert "error=" in response.headers["location"]
        assert "stockroom_session" not in client.cookies


def test_the_session_cookie_is_httponly_and_samesite(app, admin):
    with TestClient(app) as client:
        token = csrf(client, "/login")
        response = client.post(
            "/login",
            data={"email": "carter@rit.edu", "password": STRONG, "_csrf": token},
            follow_redirects=False,
        )
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "samesite=strict" in cookie.lower()
        assert "Path=/" in cookie


def test_logging_out_revokes_the_session(app, admin, conn):
    client = sign_in(app, "carter@rit.edu", STRONG)
    try:
        assert client.get("/", follow_redirects=False).status_code == 200
        token = csrf(client, "/")
        client.post("/logout", data={"_csrf": token}, follow_redirects=False)
        assert client.get("/", follow_redirects=False).status_code == 303
    finally:
        client.__exit__(None, None, None)


def test_disabling_an_account_kills_its_session_immediately(
    app, conn, admin, requester
):
    client = sign_in(app, "an1234@rit.edu", OTHER)
    try:
        assert client.get("/", follow_redirects=False).status_code == 200
        accounts.set_status(conn, actor=admin.as_actor(),
                            account_id=requester.id, status="disabled")
        # Not at the next idle timeout -- now.
        assert client.get("/", follow_redirects=False).status_code == 303
    finally:
        client.__exit__(None, None, None)


def test_login_issues_a_fresh_token_each_time(app, admin):
    """Session fixation: a token from before login must not survive it."""
    first = sign_in(app, "carter@rit.edu", STRONG)
    second = sign_in(app, "carter@rit.edu", STRONG)
    try:
        assert first.cookies["stockroom_session"] != second.cookies["stockroom_session"]
    finally:
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)


def test_a_forged_session_cookie_is_rejected(app, admin):
    with TestClient(app) as client:
        client.cookies.set("stockroom_session", "a" * 43)
        assert client.get("/", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


def test_security_headers_are_present(staff_client):
    response = staff_client.get("/")
    assert "nonce-" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"


def test_the_nonce_changes_every_request(staff_client):
    def nonce(html):
        return re.search(r'nonce="([^"]+)"', html).group(1)

    assert nonce(staff_client.get("/").text) != nonce(staff_client.get("/").text)


def test_the_page_carries_no_inline_handlers(staff_client, conn, admin):
    """A strict CSP would silently break these, so they must not come back."""
    item = service.create_item(conn, actor=admin.as_actor(), name="Camera", quantity=1)
    for path in ("/", "/items", f"/items/{item.id}", "/requests", "/accounts"):
        body = staff_client.get(path).text
        assert "onclick=" not in body, path
        assert "onsubmit=" not in body, path
        assert not re.search(r'\sstyle="', body), f"inline style in {path}"


def test_an_unknown_host_header_is_refused(app, admin):
    with TestClient(app, base_url="http://evil.test") as client:
        assert client.get("/health").status_code == 400


def test_the_error_handler_does_not_follow_an_offsite_referer(staff_client, conn, admin):
    """The StockroomError handler redirects to Referer; it must stay local."""
    item = service.create_item(conn, actor=admin.as_actor(), name="Camera", quantity=1)
    token = csrf(staff_client, f"/items/{item.id}")
    response = staff_client.post(
        f"/items/{item.id}/restore",     # not archived -> ConflictError
        data={"_csrf": token},
        headers={"referer": "https://evil.test/phish"},
        follow_redirects=False,
    )
    assert not response.headers["location"].startswith("http")
