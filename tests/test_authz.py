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
def staff(conn, admin):
    """A staff account that is NOT an administrator.

    `staff_client` below signs in the admin, because staff-or-better is what
    most routes ask for. That makes it useless for testing the boundary
    *between* the two roles, which is how a staff account kept the ability to
    switch off every administrator.
    """
    account = accounts.register(
        conn, first_name="Sam", last_name="Torres", email="st5678@rit.edu",
        password=OTHER, role="staff", status="active", actor=SETUP,
    )
    return account


@pytest.fixture
def staff_client(app, admin):
    client = sign_in(app, "carter@rit.edu", STRONG)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def staff_only_client(app, staff):
    """Signed in as staff, with no admin rights."""
    client = sign_in(app, "st5678@rit.edu", OTHER)
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


def _walk_routes(routes):
    """Yield (method, path, endpoint) for every route, however it is nested.

    An included router is not a plain list of routes. FastAPI wraps each
    `include_router` call in a `_IncludedRouter`, which has no `.path` and --
    the trap -- no `.routes` either: the real ones hang off
    `.original_router`. Reading `.routes` with a `[]` default therefore
    walked straight past all seventy-eight of this application's router
    routes and returned only the dozen declared on `app` itself, so the two
    enumerating tests below passed by finding nothing to check.

    `_route_count_is_sane` guards the traversal itself, because a walk that
    silently returns too little is exactly how the guarantee was lost.
    """
    for route in routes:
        nested = getattr(route, "routes", None)
        if nested is None:
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", None) if included else None
        if getattr(route, "path", None) is None:
            yield from _walk_routes(nested or [])
            continue
        for method in getattr(route, "methods", set()) or set():
            yield method, route.path, getattr(route, "endpoint", None)


def _app_routes(app):
    """Every (method, path) the application serves, flattened."""
    return [(method, path) for method, path, _ in _walk_routes(app.routes)]


def test_the_route_walk_actually_finds_the_routes(app):
    """The enumerating tests are only worth anything if the walk works.

    They assert that nothing in a list is unguarded, so an empty list passes
    them. It did: `_app_routes` quietly returned 13 routes out of ~90 for as
    long as FastAPI has wrapped included routers, and every route in every
    `routes_*.py` module went unchecked. A count is a blunt instrument, but
    it fails loudly when the traversal stops seeing the application.
    """
    found = _app_routes(app)
    paths = {path for _, path in found}
    assert len(found) > 80, f"the walk found only {len(found)} routes: {sorted(paths)}"
    # A route from each router, so a partial traversal is caught too.
    for path in ("/items/new", "/loans", "/counter", "/kits", "/stocktake",
                 "/accounts", "/people", "/history", "/reports", "/requests",
                 "/login", "/publish"):
        assert path in paths, f"{path} is missing from the walked route table"


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
             "/labels", "/export.csv", "/diagnostics", "/counter",
             "/counter/return", "/kits", "/stocktake", "/reports"],
)
def test_a_requester_cannot_reach_staff_pages(requester_client, path):
    assert requester_client.get(path, follow_redirects=False).status_code == 403


@pytest.mark.parametrize(
    "path", ["/accounts", "/history", "/people", "/loans", "/requests",
             "/labels", "/export.csv", "/diagnostics", "/counter",
             "/counter/return", "/kits", "/stocktake", "/reports"],
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


def _staff_only_routes(app):
    """Every (method, path) whose handler asks for staff or admin.

    Read off the handlers themselves rather than from a list kept by hand,
    for the same reason the two enumerating tests above walk the route table:
    a list maintained separately from the code drifts, and the drift is
    invisible. `require_staff` and `require_admin` are called in the body
    rather than declared as dependencies, so the source is where the answer
    is.
    """
    import inspect

    found = []
    for method, path, endpoint in _walk_routes(app.routes):
        if method not in ("GET", "POST") or endpoint is None:
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):  # pragma: no cover - not a Python handler
            continue
        if "require_staff(" in source or "require_admin(" in source:
            found.append((method, path))
    return found


def test_every_staff_only_route_refuses_a_requester(app, requester_client, conn, admin):
    """The role boundary, enforced over the whole route table.

    `test_a_requester_cannot_reach_staff_pages` names thirteen paths; this
    finds every route that asks for staff, GET and POST alike, and proves a
    requester is turned away from all of them. A CSRF token is supplied, so a
    403 here is an authorisation refusal and not the CSRF gate firing first.
    """
    service.create_item(conn, actor=admin.as_actor(), name="Camera", quantity=2)
    token = csrf(requester_client, "/")

    staff_only = _staff_only_routes(app)
    # If this ever comes back empty the test is passing vacuously.
    assert len(staff_only) > 40, f"only found {len(staff_only)} staff routes"

    # 403 is the guard refusing. 422 is FastAPI rejecting the form body before
    # the handler runs at all, which happens on the staff POSTs that have
    # required fields -- this test sends none of them. Both are refusals and
    # neither reaches the service layer, and
    # `test_no_route_does_work_before_its_permission_check` is what makes the
    # second one safe rather than merely lucky.
    leaked = []
    for method, path in staff_only:
        response = requester_client.request(
            method, _concrete(path), data={"_csrf": token}, follow_redirects=False
        )
        if response.status_code not in (403, 422):
            leaked.append(f"{method} {path} -> {response.status_code}")
    assert not leaked, "a requester was not refused by these staff routes: " + str(leaked)


def test_no_route_does_work_before_its_permission_check():
    """A handler's permission check must be the first thing it does.

    Permissions are checked in the body here, not declared as dependencies,
    so "is it guarded?" and "is it guarded *before* anything happens?" are
    two different questions and only the first is visible in a route listing.
    A read, a write or a service call above the guard would run for a caller
    who is about to be refused.

    Docstrings and local imports are allowed above it; nothing else is.
    """
    import ast
    import pathlib

    web = pathlib.Path(__file__).resolve().parent.parent / "src" / "stockroom" / "web"
    offenders = []
    checked = 0
    for source_file in sorted(web.glob("routes_*.py")) + [web / "app.py"]:
        tree = ast.parse(source_file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and getattr(d.func.value, "id", "") in ("router", "app")
                for d in node.decorator_list
            ):
                continue
            guard_at = None
            for index, statement in enumerate(node.body):
                if any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id.startswith("require_")
                    for sub in ast.walk(statement)
                ):
                    guard_at = index
                    break
            if guard_at is None:
                continue          # public route; the enumerating tests cover it
            checked += 1
            for statement in node.body[:guard_at]:
                if isinstance(statement, ast.Expr) and isinstance(
                    statement.value, ast.Constant
                ):
                    continue      # docstring
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    continue
                offenders.append(f"{source_file.name}:{node.name}")
                break

    assert checked > 60, f"only inspected {checked} guarded handlers"
    assert not offenders, "these handlers act before checking permission: " + str(offenders)


def _links_on(html: str) -> set[str]:
    """Local paths a browser would follow or submit to on this page."""
    found = set()
    for match in re.finditer(r'(?:href|action)="(/[^"#]*)"', html):
        target = match.group(1).split("?")[0].rstrip("/") or "/"
        found.add(target)
    return found


def test_no_page_a_requester_can_see_links_to_a_staff_page(
    requester_client, conn, admin, requester
):
    """Every link and form on a requester's pages must lead somewhere they may go.

    A control that 403s is not a security hole -- the route refuses it -- but
    it is a broken page, and it is the symptom of the real bug: item_detail
    once rendered the whole staff console, borrower names and email addresses
    included, to anyone signed in. The route withholds that data now. This
    walks what a requester can actually reach and follows every link it
    finds, so the next staff control dropped into a shared template fails
    here rather than in front of a student.
    """
    from stockroom import requests_service

    item = service.create_item(
        conn, actor=admin.as_actor(), name="Camera", quantity=2, tracked=True
    )
    service.checkout(
        conn, actor=admin.as_actor(), item_id=item.id,
        person_email="victim@rit.edu", person_name="Victim Person", quantity=1,
    )
    mine = requests_service.submit_new_item(
        conn, actor=requester.as_actor(), requester_id=requester.id, name="A gel kit"
    )

    start = [
        "/", "/items", f"/items/{item.id}", "/account", "/requests/mine",
        f"/requests/{mine.id}", "/requests/new/borrow", "/requests/new/new_item",
        "/requests/new/open_hours",
    ]

    broken = []
    for path in start:
        page_response = requester_client.get(path, follow_redirects=False)
        assert page_response.status_code == 200, f"{path} -> {page_response.status_code}"
        for target in _links_on(page_response.text):
            if deps.is_public_path(target) or target.startswith("/static"):
                continue
            followed = requester_client.get(target, follow_redirects=False)
            if followed.status_code == 403:
                broken.append(f"{path} links to {target}")
    assert not broken, "requester pages link to staff-only pages: " + str(broken)


def test_an_item_page_does_not_show_a_requester_who_has_one(
    requester_client, staff_client, conn, admin
):
    """Borrower identity is staff-only, on the page as well as in the payload.

    The public page omits borrowers unless PUBLIC_SHOW_BORROWERS is turned on
    (see publish/render.py); a signed-in requester is not a reason to be
    laxer than the page pinned on the corridor wall. The borrower datalist on
    this page was every email address the stockroom holds.
    """
    item = service.create_item(
        conn, actor=admin.as_actor(), name="Camera", quantity=2, tracked=True
    )
    service.checkout(
        conn, actor=admin.as_actor(), item_id=item.id,
        person_email="victim@rit.edu", person_name="Victim Person", quantity=1,
    )

    body = requester_client.get(f"/items/{item.id}").text
    assert body.count("Camera"), "the requester should still see the item itself"
    assert "victim@rit.edu" not in body
    assert "Victim Person" not in body

    # Staff still get the whole picture -- this is a split, not a removal.
    staff_body = staff_client.get(f"/items/{item.id}").text
    assert "victim@rit.edu" in staff_body
    assert "Victim Person" in staff_body


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


def test_staff_cannot_switch_off_an_administrator(
    staff_only_client, conn, admin, requester
):
    """The route that used to be the way around require_admin on /role.

    A staff account could not demote an administrator, but could disable one
    -- and then the next, until an installation had no administrator at all
    and nothing short of shell access to the Pi could grant the role back.
    """
    token = csrf(staff_only_client, "/accounts")

    assert staff_only_client.post(
        f"/accounts/{admin.id}/status", data={"status": "disabled", "_csrf": token},
        follow_redirects=False,
    ).status_code == 403
    assert accounts.get_account(conn, admin.id).status == "active"

    # Declining a signup is routine staff work and stays with staff.
    assert staff_only_client.post(
        f"/accounts/{requester.id}/status",
        data={"status": "disabled", "_csrf": token}, follow_redirects=False,
    ).status_code == 303
    assert accounts.get_account(conn, requester.id).status == "disabled"


def test_staff_cannot_switch_off_another_staff_account(
    staff_only_client, conn, admin
):
    other = accounts.register(
        conn, first_name="Robin", last_name="Fell", email="rf4321@rit.edu",
        password=OTHER, role="staff", status="active", actor=SETUP,
    )
    token = csrf(staff_only_client, "/accounts")
    assert staff_only_client.post(
        f"/accounts/{other.id}/status", data={"status": "disabled", "_csrf": token},
        follow_redirects=False,
    ).status_code == 403
    assert accounts.get_account(conn, other.id).status == "active"


def test_an_admin_can_still_switch_off_another_admin(staff_client, conn, admin):
    """The guard is about privilege, not about admins being untouchable."""
    other = accounts.register(
        conn, first_name="Dana", last_name="Iyer", email="di8765@rit.edu",
        password=OTHER, role="admin", status="active", actor=SETUP,
    )
    token = csrf(staff_client, "/accounts")
    assert staff_client.post(
        f"/accounts/{other.id}/status", data={"status": "disabled", "_csrf": token},
        follow_redirects=False,
    ).status_code == 303
    assert accounts.get_account(conn, other.id).status == "disabled"


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


def test_a_return_cannot_redirect_off_site(staff_client, conn):
    """The `next` field on a return form is attacker-controlled.

    Handed to redirect() unwrapped it turned this route into a launch pad for
    a phishing link: a POST with next=https://evil.example/ answered 303 to
    that host, carrying the site's own flash message with it.
    """
    item = service.create_item(conn, actor=SETUP, name="Tripod", quantity=2)
    person = service.create_person(conn, actor=SETUP, name="Al", email="al@rit.edu")
    loan = service.checkout(conn, actor=SETUP, item_id=item.id, person_id=person.id)

    response = staff_client.post(
        f"/loans/{loan.id}/return",
        data={"_csrf": csrf(staff_client, "/loans"),
              "next": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not response.headers["location"].startswith("http"), \
        f"redirected off-site to {response.headers['location']}"
    assert response.headers["location"].startswith("/loans")


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


@pytest.mark.parametrize(
    "make_request",
    [
        pytest.param(
            lambda c: c.get("/items", follow_redirects=False),
            id="anonymous-redirect-to-login",
        ),
        pytest.param(
            lambda c: c.post("/items/new", data={"name": "x"},
                             follow_redirects=False),
            id="refused-post",
        ),
    ],
)
def test_a_refusal_is_hardened_like_any_other_response(app, admin, make_request):
    """The pages a hostile request actually reaches must carry the headers too.

    require_authentication used to sit OUTSIDE the middleware that adds them,
    so its 401 page and its /login redirect went out bare; and the CSRF and
    oversize refusals returned early from inside that middleware, skipping the
    same step. Between them that was every refusal the application makes.
    """
    with TestClient(app) as anonymous:
        response = make_request(anonymous)

    assert response.status_code in (303, 401, 403)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    if response.headers.get("content-type", "").startswith("text/html"):
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_host_check_runs_before_anything_else(app, admin):
    """A forged Host must be refused on a protected path too.

    The Host check was the innermost of the three middlewares, so an anonymous
    request to a protected path was redirected to /login and never reached it.
    Both existing Host tests use /health, which is public, so neither noticed.
    """
    with TestClient(app) as anonymous:
        response = anonymous.get(
            "/items", headers={"host": "evil.example"}, follow_redirects=False
        )
    assert response.status_code == 400, \
        "the Host check is being reached after the authentication gate"
    assert "STOCKROOM_ALLOWED_HOSTS" in response.text


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
