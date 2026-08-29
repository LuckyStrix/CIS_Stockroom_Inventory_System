"""The web UI, driven through real HTTP requests.

These go through the actual routes, forms and templates -- if a template
references a variable a route does not pass, these fail.
"""

import re

import pytest
from fastapi.testclient import TestClient

from stockroom import service


@pytest.fixture
def client(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as test_client:
        # The "who are you?" cookie every operator sets on first use.
        test_client.cookies.set("stockroom_operator", "Test Operator|operator@rit.edu")
        yield test_client


@pytest.fixture
def stocked(client):
    """One item with 10 units, created through the UI itself."""
    response = client.post(
        "/items/new",
        data={"name": "Canon EOS R5", "description": "Mirrorless body",
              "quantity": "10", "unit": "Unit A", "shelf": "Shelf 1",
              "sub_location": "Pelican", "min_quantity": "2",
              "barcode": "", "product_url": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(re.search(r"/items/(\d+)", response.headers["location"]).group(1))


# -- pages render ----------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/", "/items", "/loans", "/people", "/history", "/whoami", "/labels",
     "/export.csv", "/health"],
)
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_item_page_renders(client, stocked):
    body = client.get(f"/items/{stocked}").text
    assert "Canon EOS R5" in body
    assert "Unit A / Shelf 1 / Pelican" in body
    assert "CIS-000001" in body


def test_health_reports_real_numbers(client, stocked):
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["item_count"] == 1
    assert payload["total_units"] == 10


# -- identity --------------------------------------------------------------
def test_changing_things_requires_identifying_yourself(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as anonymous:
        response = anonymous.get("/items/new", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/whoami")


def test_setting_your_name_then_continuing(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as fresh:
        response = fresh.post(
            "/whoami",
            data={"name": "Carter", "email": "carter@rit.edu", "next": "/items"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/items")
        assert "Carter" in fresh.cookies["stockroom_operator"]


def test_whoami_will_not_redirect_off_site(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as fresh:
        response = fresh.post(
            "/whoami",
            data={"name": "Carter", "email": "", "next": "https://evil.test/"},
            follow_redirects=False,
        )
        assert not response.headers["location"].startswith("http")


def test_the_actor_reaches_the_audit_log(client, stocked, temp_env):
    from stockroom import db

    event = service.list_events(db.connect(), item_id=stocked)[0]
    assert event.actor == "Test Operator <operator@rit.edu>"


def test_sso_headers_take_precedence_over_the_cookie(client, stocked, temp_env):
    """The seam that RIT Shibboleth will plug into (docs/sso-integration.md)."""
    from stockroom import db

    client.post(
        f"/items/{stocked}/edit",
        data={"name": "Canon EOS R5", "description": "via sso", "quantity": "10",
              "unit": "Unit A", "shelf": "Shelf 1", "sub_location": "Pelican",
              "min_quantity": "2", "barcode": "CIS-000001", "product_url": ""},
        headers={"X-Shib-DisplayName": "Real Person", "X-Shib-Mail": "rp1234@rit.edu"},
    )
    event = service.list_events(db.connect(), item_id=stocked)[0]
    assert event.actor == "Real Person <rp1234@rit.edu>"


# -- the checkout flow -----------------------------------------------------
def test_checkout_then_partial_return(client, stocked):
    response = client.post(
        f"/items/{stocked}/checkout",
        data={"person_email": "alice@rit.edu", "person_name": "Alice Nguyen",
              "quantity": "3", "due_at": "", "note": "senior project"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Checked out 3 x Canon EOS R5 to Alice Nguyen" in response.text

    page = client.get(f"/items/{stocked}").text
    assert "Alice Nguyen" in page
    assert re.search(r"<strong>7</strong>\s*of\s*10", page), "availability should read 7 of 10"

    from stockroom import db
    loan = service.list_loans(db.connect(), open_only=True)[0]
    client.post(f"/loans/{loan.id}/return",
                data={"quantity": "1", "next": f"/items/{stocked}"})

    item = service.get_item(db.connect(), stocked)
    assert item.available == 8
    assert item.out_qty == 2


def test_over_checkout_shows_a_message_not_a_crash(client, stocked):
    response = client.post(
        f"/items/{stocked}/checkout",
        data={"person_email": "alice@rit.edu", "person_name": "Alice",
              "quantity": "99", "due_at": "", "note": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "available" in response.text
    assert "flash error" in response.text


def test_a_due_date_survives_to_the_overdue_list(client, stocked):
    client.post(
        f"/items/{stocked}/checkout",
        data={"person_email": "alice@rit.edu", "person_name": "Alice",
              "quantity": "1", "due_at": "2020-01-01", "note": ""},
    )
    assert "Overdue" in client.get("/loans?filter=overdue").text


def test_a_new_borrower_is_created_by_checking_out(client, stocked):
    client.post(
        f"/items/{stocked}/checkout",
        data={"person_email": "newbie@rit.edu", "person_name": "New Bie",
              "quantity": "1", "due_at": "", "note": ""},
    )
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
    client.post(
        f"/items/{stocked}/edit",
        data={"name": "Canon EOS R5", "description": "Mirrorless body",
              "quantity": "10", "unit": "Unit C", "shelf": "Shelf 9",
              "sub_location": "", "min_quantity": "2",
              "barcode": "CIS-000001", "product_url": ""},
    )
    history = client.get(f"/history?item_id={stocked}").text
    assert "item.relocate" in history
    assert "Unit C / Shelf 9" in history


def test_archiving_hides_the_item_from_the_list(client, stocked):
    client.post(f"/items/{stocked}/archive")
    assert "Canon EOS R5" not in client.get("/items").text
    assert "Canon EOS R5" in client.get("/items?filter=archived").text


def test_archiving_a_lent_item_is_refused(client, stocked):
    client.post(f"/items/{stocked}/checkout",
                data={"person_email": "a@rit.edu", "person_name": "A",
                      "quantity": "1", "due_at": "", "note": ""})
    response = client.post(f"/items/{stocked}/archive", follow_redirects=True)
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


# -- the public page is served --------------------------------------------
def test_the_public_page_is_served_and_excludes_borrowers(client, stocked):
    client.post(f"/items/{stocked}/checkout",
                data={"person_email": "alice@rit.edu", "person_name": "Alice Nguyen",
                      "quantity": "2", "due_at": "", "note": ""})
    client.post("/publish")

    body = client.get("/public/index.html").text
    assert "Canon EOS R5" in body
    assert "Alice Nguyen" not in body
    assert "alice@rit.edu" not in body
