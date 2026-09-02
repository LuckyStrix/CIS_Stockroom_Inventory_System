"""The inventory web UI, driven through real HTTP as a signed-in staff member.

These exercise the actual routes, forms and templates, so a template that
references a variable its route does not pass fails here rather than in the
stockroom. Authentication and authorisation have their own file
(`test_authz.py`); this one is about the inventory workflows.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stockroom import accounts, db, service
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
     "/health", "/requests", "/requests/mine", "/accounts", "/account",
     "/diagnostics"],
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
    assert set(payload) == {
        "status", "version", "schema_version", "item_count", "audit_head",
    }


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


def test_the_app_does_not_override_the_public_pages_own_policy(client, stocked):
    """The generated page must not be handed the app's nonce CSP as well.

    It is a static file with no nonce -- it carries a <meta> CSP built from
    hashes of its own inline blocks instead. A browser enforces every policy
    it receives and takes the intersection, so sending `script-src 'self'
    'nonce-...'` alongside the hash policy allowed neither the stylesheet nor
    the script: an unstyled page with an empty table, and nothing in the
    response to say why.
    """
    post(client, "/publish", {}, form_page="/")
    response = client.get("/public/index.html")

    assert "content-security-policy" not in response.headers, (
        "the app's nonce policy is being stamped on the generated page, "
        "which cannot satisfy it"
    )
    # The page's own policy is still there, and the rest of the hardening
    # applies to it exactly as before.
    assert "Content-Security-Policy" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_the_generated_pages_policy_covers_its_own_blocks(temp_env, conn):
    """Every inline block on the page must be named by the page's own CSP.

    Recomputed here from the served bytes rather than trusted from render.py,
    because a hash that does not match what actually shipped is a page that
    silently does nothing.
    """
    import base64
    import hashlib
    import html as html_module

    from stockroom.publish.render import render_site

    page_html = render_site(conn)["index.html"]
    policy = html_module.unescape(
        re.search(
            r'<meta http-equiv="Content-Security-Policy" content="([^"]*)"',
            page_html,
        ).group(1)
    )

    def digest(block):
        return "'sha256-" + base64.b64encode(
            hashlib.sha256(block.encode()).digest()
        ).decode() + "'"

    styles = re.findall(r"<style[^>]*>(.*?)</style>", page_html, re.S)
    scripts = [
        body for attrs, body in re.findall(
            r"<script([^>]*)>(.*?)</script>", page_html, re.S
        )
        if "type=" not in attrs          # the JSON data block is inert
    ]
    assert styles and scripts, "the page should have exactly the blocks we hash"

    for block in styles:
        assert digest(block) in policy, "an inline <style> is not in style-src"
    for block in scripts:
        assert digest(block) in policy, "an inline <script> is not in script-src"


def test_the_label_sheets_print_button_can_actually_run(client, stocked):
    """labels.html bypassed page(), so csp_nonce was undefined.

    The page shipped `<script nonce="">` against a header naming a real
    nonce, so the browser refused the only script on the page -- and the only
    thing the page is for is pressing Print.
    """
    post(client, f"/items/{stocked}/barcode", {}, form_page=f"/items/{stocked}")
    response = client.get("/labels")

    assert response.status_code == 200
    nonce = re.search(r'<script nonce="([^"]*)"', response.text).group(1)
    assert nonce, "the label sheet's script has an empty nonce"
    assert f"'nonce-{nonce}'" in response.headers["content-security-policy"], \
        "the nonce in the page does not match the one in the policy"


# ---------------------------------------------------------------------------
# The CSP is only worth having if the templates actually live within it.
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "src" / "stockroom" / "templates"

# public.html is generated, written to disk and served as a static file. It
# carries its own <meta> CSP built from SHA-256 hashes of its own inline
# blocks (see publish/render._csp_hashes), because a file opened from a USB
# stick or GitHub Pages has no server to mint a nonce for it.
_GENERATED = {"public.html"}


@pytest.mark.parametrize(
    "template",
    sorted(p.name for p in _TEMPLATE_DIR.glob("*.html")),
)
def test_no_template_carries_inline_css(template):
    """The app sends `style-src 'self'` with no 'unsafe-inline'.

    A browser drops an inline <style> block or style="" attribute silently --
    no console error, no failed request, just an unstyled page. That is how
    labels.html spent a while printing its Avery sheet with none of its layout
    applied, and why this is a test rather than a note in a README.
    """
    if template in _GENERATED:
        pytest.skip("generated file with its own hash-based CSP")
    body = (_TEMPLATE_DIR / template).read_text()
    assert "<style" not in body, f"{template} has an inline <style> block"
    assert "style=" not in body, f"{template} has an inline style attribute"


@pytest.mark.parametrize(
    "template",
    sorted(p.name for p in _TEMPLATE_DIR.glob("*.html")),
)
def test_every_script_in_a_served_template_carries_the_nonce(template):
    """Same again for `script-src 'self' 'nonce-...'`."""
    if template in _GENERATED:
        pytest.skip("generated file with its own hash-based CSP")
    for tag in re.findall(r"<script[^>]*>", (_TEMPLATE_DIR / template).read_text()):
        assert "nonce=" in tag, f"{template} has a script without a nonce: {tag}"


def test_a_duplicate_class_attribute_is_never_left_in_a_template():
    """The second one is silently discarded, so the styling just goes missing."""
    offenders = [
        p.name for p in _TEMPLATE_DIR.glob("*.html")
        if re.search(r'<[^>]*\sclass="[^"]*"[^>]*\sclass="', p.read_text())
    ]
    assert not offenders, f"duplicate class attributes in: {offenders}"


# ---------------------------------------------------------------------------
# Condition, through the actual forms.
# ---------------------------------------------------------------------------


@pytest.fixture
def tracked_item(client):
    """An item whose individual units are tracked, created through the UI."""
    response = post(
        client, "/items/new",
        {"name": "Canon EOS R5", "description": "Mirrorless body",
         "quantity": "4", "unit": "Unit A", "shelf": "Shelf 1",
         "sub_location": "", "min_quantity": "", "barcode": "",
         "product_url": "", "tracked": "1"},
        form_page="/items/new", follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    return int(re.search(r"/items/(\d+)", response.headers["location"]).group(1))


def test_the_item_page_renders_the_condition_card(client, stocked):
    body = client.get(f"/items/{stocked}").text
    assert "Record a problem" in body
    assert "Take out of service" in body


def test_taking_units_out_of_service_through_the_form(client, stocked):
    response = post(
        client, f"/items/{stocked}/holds",
        {"state": "broken", "quantity": "3", "note": "water damage"},
        form_page=f"/items/{stocked}", follow_redirects=True,
    )
    assert response.status_code == 200

    body = client.get(f"/items/{stocked}").text
    assert "water damage" in body
    assert "Broken" in body

    item = service.get_item(db.connect(), stocked)
    assert item.available == 7
    assert item.quantity == 10, "still ten owned"


def test_the_availability_breakdown_is_shown(client, stocked):
    post(client, f"/items/{stocked}/holds",
         {"state": "gone", "quantity": "2", "note": "never came back"},
         form_page=f"/items/{stocked}", follow_redirects=True)

    body = client.get(f"/items/{stocked}").text
    assert "Out of service" in body
    assert "unaccounted for" in body


def test_a_hold_can_be_moved_along_and_closed(client, stocked):
    post(client, f"/items/{stocked}/holds", {"state": "broken", "quantity": "1"},
         form_page=f"/items/{stocked}", follow_redirects=True)
    hold = service.list_holds(db.connect(), item_id=stocked)[0]

    post(client, f"/holds/{hold.id}/state", {"state": "repair"},
         form_page=f"/items/{stocked}", follow_redirects=True)
    assert service.get_hold(db.connect(), hold.id).state == "repair"

    post(client, f"/holds/{hold.id}/close", {"resolution": "fixed under warranty"},
         form_page=f"/items/{stocked}", follow_redirects=True)
    assert service.get_item(db.connect(), stocked).available == 10


def test_registering_and_breaking_an_individual_unit(client, tracked_item):
    for tag in ("CIS-U-1", "CIS-U-2"):
        post(client, f"/items/{tracked_item}/units",
             {"asset_tag": tag, "serial": f"SN-{tag}", "note": ""},
             form_page=f"/items/{tracked_item}", follow_redirects=True)

    body = client.get(f"/items/{tracked_item}").text
    assert "Individual units" in body
    assert "CIS-U-1" in body and "CIS-U-2" in body

    unit = service.get_unit_by_asset_tag(db.connect(), "CIS-U-2")
    post(client, f"/items/{tracked_item}/holds",
         {"state": "broken", "unit_id": str(unit.id), "note": "bent lens mount"},
         form_page=f"/items/{tracked_item}", follow_redirects=True)

    body = client.get(f"/items/{tracked_item}").text
    assert "bent lens mount" in body
    assert service.get_item(db.connect(), tracked_item).available == 3


def test_a_duplicate_asset_tag_is_refused_with_a_message(client, tracked_item):
    post(client, f"/items/{tracked_item}/units", {"asset_tag": "DUP", "serial": ""},
         form_page=f"/items/{tracked_item}", follow_redirects=True)
    response = post(client, f"/items/{tracked_item}/units",
                    {"asset_tag": "DUP", "serial": ""},
                    form_page=f"/items/{tracked_item}", follow_redirects=True)
    assert "already belongs" in response.text


def test_returning_something_damaged_in_one_step(client, stocked):
    """The counter case: the loan closes and the hold opens together."""
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "alice@rit.edu", "person_name": "Alice Nguyen",
          "quantity": "2", "due_at": "", "note": ""},
         form_page=f"/items/{stocked}", follow_redirects=True)
    loan = service.list_loans(db.connect(), item_id=stocked, open_only=True)[0]

    response = post(
        client, f"/loans/{loan.id}/return",
        {"quantity": "2", "note": "one card will not mount",
         "condition": "broken", "next": f"/items/{stocked}"},
        form_page=f"/items/{stocked}", follow_redirects=True,
    )
    assert "marked broken" in response.text

    item = service.get_item(db.connect(), stocked)
    assert (item.out_qty, item.held_qty, item.available) == (0, 2, 8)

    holds = service.list_holds(db.connect(), item_id=stocked)
    assert holds[0].loan_id == loan.id
    assert holds[0].borrower_name == "Alice Nguyen"


def test_the_return_form_offers_a_condition(client, stocked):
    post(client, f"/items/{stocked}/checkout",
         {"person_email": "bob@rit.edu", "person_name": "Bob", "quantity": "1",
          "due_at": "", "note": ""},
         form_page=f"/items/{stocked}", follow_redirects=True)

    body = client.get(f"/items/{stocked}").text
    assert 'name="condition"' in body
    assert ">Fine<" in body


# ---------------------------------------------------------------------------
# the Host check
# ---------------------------------------------------------------------------
#
# A real failure: opening the site from a phone on the stockroom LAN returned
# "Invalid host header" and nothing else. The allow list was the literal string
# `cis-stockroom,...`, so a Pi imaged under any other name -- or reached at its
# IP, which is what docs/raspberry-pi-setup.md tells you to do when `.local`
# does not resolve -- was refused, with no clue as to which header, which hosts
# were acceptable, or where the list lives.


def test_a_request_for_this_machines_hostname_is_accepted(client):
    """The default list follows the machine, not a name someone hoped for."""
    import socket

    host = socket.gethostname().split(".")[0]
    assert client.get("/health", headers={"host": host}).status_code == 200
    assert client.get("/health", headers={"host": f"{host}.local"}).status_code == 200


def test_the_qualified_name_is_accepted(monkeypatch):
    """The name people actually type.

    A real failure: the Pi was registered as cisstockroom.device.rit.edu, and
    the default list -- built from `gethostname().split(".")[0]` -- held
    `cisstockroom` and `cisstockroom.local` only. Every browser using the DNS
    record got "Invalid host header" from a check that was written to stop
    exactly that.
    """
    import socket
    from stockroom import config

    monkeypatch.setattr(socket, "gethostname", lambda: "cisstockroom")
    monkeypatch.setattr(socket, "getfqdn", lambda: "cisstockroom.device.rit.edu")

    hosts = config._default_allowed_hosts()
    assert "cisstockroom.device.rit.edu" in hosts
    assert "cisstockroom" in hosts, "the short name still has to work"
    assert "cisstockroom.local" in hosts, "and so does mDNS"


def test_a_qualified_hostname_is_not_given_a_local_suffix(monkeypatch):
    """When `hostname` itself is qualified, the short name is a truncation of
    it -- not the whole thing with `.local` stapled on the end."""
    import socket
    from stockroom import config

    monkeypatch.setattr(socket, "gethostname", lambda: "cisstockroom.device.rit.edu")
    monkeypatch.setattr(socket, "getfqdn", lambda: "cisstockroom.device.rit.edu")

    hosts = config._default_allowed_hosts()
    assert "cisstockroom.device.rit.edu" in hosts
    assert "cisstockroom.local" in hosts
    assert "cisstockroom.device.rit.edu.local" not in hosts


@pytest.mark.parametrize("reported", ["localhost", "127.0.0.1", "cis-stockroom"])
def test_an_unqualified_machine_gains_nothing(monkeypatch, reported):
    """getfqdn() falls back to junk on a machine with no domain -- the short
    name again, "localhost", or a bare address. None of it belongs on the list
    as a *name*, and none of it must displace the short name that does."""
    import socket
    from stockroom import config

    monkeypatch.setattr(socket, "gethostname", lambda: "cis-stockroom")
    monkeypatch.setattr(socket, "getfqdn", lambda: reported)

    hosts = config._default_allowed_hosts()
    assert hosts[:2] == ["cis-stockroom", "cis-stockroom.local"]
    assert "localhost" in hosts  # from the loopback tail, not from getfqdn()
    assert not any(h.startswith("127.0.0.1.") for h in hosts)


def test_a_resolver_failure_does_not_break_startup(monkeypatch):
    """config is imported by every entry point, including the CLI. A Pi whose
    resolver is unreachable must still boot the service."""
    import socket
    from stockroom import config

    def boom():
        raise OSError("no resolver")

    monkeypatch.setattr(socket, "gethostname", lambda: "cis-stockroom")
    monkeypatch.setattr(socket, "getfqdn", boom)

    assert "cis-stockroom" in config._default_allowed_hosts()


@pytest.mark.parametrize("host", ["10.14.2.31", "192.168.1.50:443", "[fe80::1]:443"])
def test_a_bare_ip_address_is_accepted(client, host):
    """The documented fallback when mDNS does not work, and the only route
    into the Pi from a device with no `.local` resolver."""
    assert client.get("/health", headers={"host": host}).status_code == 200


def test_an_unknown_host_is_still_rejected(client):
    """The check is still a check -- this is not `allowed_hosts=["*"]`."""
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400


def test_the_rejection_says_what_to_do_about_it(client):
    """The whole reason Starlette's middleware was replaced."""
    body = client.get("/health", headers={"host": "evil.example"}).text
    assert "evil.example" in body, "does not say which host was rejected"
    assert "STOCKROOM_ALLOWED_HOSTS" in body, "does not say where to fix it"
    assert "systemctl restart stockroom" in body


def test_the_rejection_does_not_reflect_markup(client):
    """It is rendered above the middleware that sets X-Content-Type-Options."""
    body = client.get(
        "/health", headers={"host": "<script>alert(1)</script>"}).text
    assert "<script>" not in body


def test_an_empty_host_is_rejected(client):
    from stockroom.web.app import _host_is_allowed

    assert not _host_is_allowed("")


def test_ip_hosts_can_be_switched_off(monkeypatch):
    from stockroom import config
    from stockroom.web.app import _host_is_allowed

    assert _host_is_allowed("10.14.2.31")
    monkeypatch.setattr(config, "ALLOW_IP_HOSTS", False)
    assert not _host_is_allowed("10.14.2.31")


def test_a_configured_wildcard_still_matches_subdomains(monkeypatch):
    """Starlette's behaviour, kept: an operator's existing value must not
    quietly change meaning."""
    from stockroom import config
    from stockroom.web.app import _host_is_allowed

    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["*.cis.rit.edu"])
    assert _host_is_allowed("stockroom.cis.rit.edu")
    assert not _host_is_allowed("cis.rit.edu.evil.example")


def test_loopback_survives_an_explicit_allow_list():
    """The installer's health check and `stockroom doctor` reach the app at
    127.0.0.1; an operator narrowing the list must not lock the Pi out of
    itself."""
    from stockroom.config import _allowed_hosts

    hosts = _allowed_hosts("stockroom.cis.rit.edu, cis-stockroom")
    assert hosts[:2] == ["stockroom.cis.rit.edu", "cis-stockroom"]
    assert {"localhost", "127.0.0.1"} <= set(hosts)


def test_an_unset_allow_list_follows_the_hostname():
    import socket

    from stockroom.config import _allowed_hosts

    host = socket.gethostname().split(".")[0].lower()
    assert _allowed_hosts("") == _allowed_hosts("   ") == [
        host, f"{host}.local", "localhost", "127.0.0.1", "::1", "testserver"]
