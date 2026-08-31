"""The inventory web UI, driven through real HTTP as a signed-in staff member.

These exercise the actual routes, forms and templates, so a template that
references a variable its route does not pass fails here rather than in the
stockroom. Authentication and authorisation have their own file
(`test_authz.py`); this one is about the inventory workflows.
"""

import re

import pytest
from fastapi.testclient import TestClient

from stockroom import accounts, service
from stockroom.service import Actor

SETUP = Actor("cli:test")
STAFF_PASSWORD = "glass onion tuesday lamp"


@pytest.fixture
def client(temp_env):
    """A signed-in staff session."""
    from stockroom.web.app import app

    with TestClient(app) as test_client:
        from stockroom import db

        accounts.register(
            db.connect(), first_name="Test", last_name="Operator",
            email="operator@rit.edu", password=STAFF_PASSWORD,
            role="staff", status="active", actor=SETUP,
        )
        token = csrf(test_client, "/login")
        response = test_client.post(
            "/login",
            data={"email": "operator@rit.edu", "password": STAFF_PASSWORD,
                  "next": "/", "_csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text[:300]
        yield test_client


def csrf(client: TestClient, path: str) -> str:
    """The CSRF token from a rendered page, as a browser would submit it."""
    match = re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text)
    return match.group(1) if match else ""


def post(client: TestClient, path: str, data: dict, *, form_page: str | None = None,
         **kwargs):
    """POST with the CSRF token taken from the page the form lives on."""
    payload = dict(data)
    payload["_csrf"] = csrf(client, form_page or path)
    return client.post(path, data=payload, **kwargs)


@pytest.fixture
def stocked(client):
    """One item with 10 units, created through the UI itself."""
    response = post(
        client, "/items/new",
        {"name": "Canon EOS R5", "description": "Mirrorless body",
         "quantity": "10", "unit": "Unit A", "shelf": "Shelf 1",
         "sub_location": "Pelican", "min_quantity": "2",
         "barcode": "", "product_url": ""},
        form_page="/items/new", follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    return int(re.search(r"/items/(\d+)", response.headers["location"]).group(1))


# -- pages render ----------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/", "/items", "/loans", "/people", "/history", "/labels", "/export.csv",
     "/health", "/requests", "/requests/mine", "/accounts", "/account"],
)
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_item_page_renders(client, stocked):
    body = client.get(f"/items/{stocked}").text
    assert "Canon EOS R5" in body
    assert "Unit A / Shelf 1 / Pelican" in body
    assert "CIS-000001" in body


def test_health_is_public_and_says_little(temp_env):
    """A liveness probe, reachable without a session and carrying no detail."""
    from stockroom.web.app import app

    with TestClient(app) as anonymous:
        payload = anonymous.get("/health").json()
    assert payload["status"] == "ok"
    assert set(payload) == {"status", "version", "schema_version", "item_count"}


def test_the_actor_reaches_the_audit_log(client, stocked, temp_env):
    from stockroom import db

    event = service.list_events(db.connect(), item_id=stocked)[0]
    assert event.actor == "Test Operator <operator@rit.edu>"


# -- the checkout flow -----------------------------------------------------
def test_checkout_then_partial_return(client, stocked, temp_env):
    from stockroom import db

    response = post(
        client, f"/items/{stocked}/checkout",
        {"person_email": "alice@rit.edu", "person_name": "Alice Nguyen",
         "quantity": "3", "due_at": "", "note": "senior project"},
        form_page=f"/items/{stocked}", follow_redirects=True,
    )
    assert "Checked out 3 x Canon EOS R5 to Alice Nguyen" in response.text

    page = client.get(f"/items/{stocked}").text
    assert "Alice Nguyen" in page
    assert re.search(r"<strong>7</strong>\s*of\s*10", page)

    loan = service.list_loans(db.connect(), open_only=True)[0]
    post(client, f"/loans/{loan.id}/return",
         {"quantity": "1", "next": f"/items/{stocked}"},
         form_page=f"/items/{stocked}")

    item = service.get_item(db.connect(), stocked)
    assert item.available == 8
    assert item.out_qty == 2


def test_over_checkout_shows_a_message_not_a_crash(client, stocked):
    response = post(
        client, f"/items/{stocked}/checkout",
        {"person_email": "alice@rit.edu", "person_name": "Alice",
         "quantity": "99", "due_at": "", "note": ""},
        form_page=f"/items/{stocked}", follow_redirects=True,
    )
    assert response.status_code == 200
    assert "available" in response.text
    assert "flash error" in response.text


def test_a_due_date_survives_to_the_overdue_list(client, stocked):
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "alice@rit.edu", "person_name": "Alice",
          "quantity": "1", "due_at": "2020-01-01", "note": ""},
         form_page=f"/items/{stocked}")
    assert "Overdue" in client.get("/loans?filter=overdue").text


def test_a_new_borrower_is_created_by_checking_out(client, stocked):
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "newbie@rit.edu", "person_name": "New Bie",
          "quantity": "1", "due_at": "", "note": ""},
         form_page=f"/items/{stocked}")
    assert "New Bie" in client.get("/people").text


# -- scanning --------------------------------------------------------------
def test_scanning_a_barcode_jumps_to_the_item(client, stocked):
    response = client.get("/scan?code=CIS-000001", follow_redirects=False)
    assert response.headers["location"] == f"/items/{stocked}"


def test_scanning_something_unknown_falls_back_to_search(client, stocked):
    response = client.get("/scan?code=UNKNOWN", follow_redirects=False)
    assert response.headers["location"].startswith("/items?q=")


# -- items, history, labels ------------------------------------------------
def test_editing_records_history_visible_in_the_ui(client, stocked):
    post(client, f"/items/{stocked}/edit",
         {"name": "Canon EOS R5", "description": "Mirrorless body",
          "quantity": "10", "unit": "Unit C", "shelf": "Shelf 9",
          "sub_location": "", "min_quantity": "2",
          "barcode": "CIS-000001", "product_url": ""},
         form_page=f"/items/{stocked}/edit")
    history = client.get(f"/history?item_id={stocked}").text
    assert "item.relocate" in history
    assert "Unit C / Shelf 9" in history


def test_archiving_hides_the_item_from_the_list(client, stocked):
    post(client, f"/items/{stocked}/archive", {}, form_page=f"/items/{stocked}")
    assert "Canon EOS R5" not in client.get("/items").text
    assert "Canon EOS R5" in client.get("/items?filter=archived").text


def test_archiving_a_lent_item_is_refused(client, stocked):
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "a@rit.edu", "person_name": "A", "quantity": "1",
          "due_at": "", "note": ""},
         form_page=f"/items/{stocked}")
    response = post(client, f"/items/{stocked}/archive", {},
                    form_page=f"/items/{stocked}", follow_redirects=True)
    assert "still checked out" in response.text


def test_labels_include_a_barcode(client, stocked):
    body = client.get(f"/labels?ids={stocked}").text
    assert "<svg" in body
    assert "CIS-000001" in body


def test_export_csv_is_downloadable(client, stocked):
    response = client.get("/export.csv")
    assert response.headers["content-disposition"].startswith("attachment")
    assert "Canon EOS R5" in response.text


def test_search_filters_the_item_list(client, stocked):
    assert "Canon EOS R5" in client.get("/items?q=canon").text
    assert "Canon EOS R5" not in client.get("/items?q=nikon").text


def test_a_missing_item_renders_a_404_page(client):
    response = client.get("/items/4242")
    assert response.status_code == 404
    assert "Not found" in response.text


# -- the public page -------------------------------------------------------
def test_the_public_page_is_served_and_excludes_borrowers(client, stocked):
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "alice@rit.edu", "person_name": "Alice Nguyen",
          "quantity": "2", "due_at": "", "note": ""},
         form_page=f"/items/{stocked}")
    post(client, "/publish", {}, form_page="/")

    body = client.get("/public/index.html").text
    assert "Canon EOS R5" in body
    assert "Alice Nguyen" not in body
    assert "alice@rit.edu" not in body


def test_the_public_page_needs_no_session(client, stocked, temp_env):
    """The whole point: availability is answerable without logging in."""
    from stockroom.web.app import app

    post(client, "/publish", {}, form_page="/")
    with TestClient(app) as anonymous:
        response = anonymous.get("/public/index.html")
    assert response.status_code == 200
    assert "Canon EOS R5" in response.text
