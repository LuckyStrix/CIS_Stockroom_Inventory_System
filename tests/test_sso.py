"""RIT single sign-on, end to end against a fake identity provider.

Two things are being proved here, and they are worth separating.

**That the protocol is implemented correctly** -- signatures, audiences,
expiry, InResponseTo. That is `saml.parse_response`, and the tests for it use
a real signed assertion from a real key, because a stubbed signature check
proves nothing about a signature check.

**That /sso/acs is safe without a CSRF token.** It is the only POST in the
application that skips one, so the thing it skipped has to be replaced by
something at least as strong. The test that matters most is
`test_an_assertion_bound_to_another_browser_is_refused`: an attacker who
authenticates at RIT as themselves, captures their own signed assertion and
posts it into your browser must not sign you in as them. A signature check
alone does not stop that -- the assertion is genuinely signed -- and neither
does InResponseTo alone, because the attacker simply never spends their own
handshake. What stops it is the state cookie.

`tests/test_authz.py` keeps its own guarantee unchanged: the enumerating test
`test_every_post_route_rejects_a_missing_csrf_token` walks /sso/acs like every
other POST and expects the same 403. This route earns it a different way.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import saml_idp  # noqa: E402

from stockroom import accounts, config, db, saml, security  # noqa: E402
from stockroom.service import Actor  # noqa: E402
from stockroom.web import deps  # noqa: E402

SETUP = Actor("cli:test")
BASE_URL = "https://cisstockroom.device.rit.edu"
ENTITY_ID = f"{BASE_URL}/shibboleth"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def keypairs(tmp_path_factory):
    """One IdP key and one SP key for the whole session.

    Generated with openssl rather than checked in, so this repository never
    contains anything shaped like a private key. openssl is not a new
    dependency -- deploy/setup-pi.sh already needs it for the Pi's TLS
    certificate.
    """
    directory = tmp_path_factory.mktemp("saml-keys")
    return (
        saml_idp.make_keypair(directory, "idp"),
        saml_idp.make_keypair(directory, "sp"),
        saml_idp.make_keypair(directory, "impostor"),
    )


@pytest.fixture
def idp(keypairs, tmp_path, monkeypatch, conn):
    """Configure the app for single sign-on and return the fake IdP's key."""
    idp_key, sp_key, _ = keypairs
    metadata = tmp_path / "rit-metadata.xml"
    metadata.write_text(saml_idp.idp_metadata(idp_key))

    monkeypatch.setattr(config, "AUTH_MODE", "sso")
    monkeypatch.setattr(config, "SSO_BASE_URL", BASE_URL)
    monkeypatch.setattr(config, "SSO_ENTITY_ID", ENTITY_ID)
    monkeypatch.setattr(config, "SSO_IDP_METADATA", metadata)
    monkeypatch.setattr(config, "SSO_SP_CERT", sp_key.cert_path)
    monkeypatch.setattr(config, "SSO_SP_KEY", sp_key.key_path)
    # The rate limiter is module state and would otherwise leak between tests.
    monkeypatch.setattr(
        "stockroom.web.routes_sso._start_throttle",
        security.RateLimiter(limit=1000, per_seconds=60),
    )
    return idp_key


@pytest.fixture
def app(temp_env):
    from stockroom.web.app import app as fastapi_app

    return fastapi_app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def start(client: TestClient, next_path: str = "/") -> tuple[str, str]:
    """Click "Sign in with RIT". Returns (request_id, relay_state)."""
    response = client.get(f"/sso/login?next={next_path}", follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    location = response.headers["location"]
    assert location.startswith(saml_idp.IDP_SSO_URL), location
    relay = parse_qs(urlparse(location).query)["RelayState"][0]
    row = db.connect().execute(
        "SELECT request_id FROM saml_auth_request WHERE relay_state = ?", (relay,)
    ).fetchone()
    return row["request_id"], relay


def finish(client: TestClient, idp_key, request_id, relay, **kwargs):
    """Post back what the identity provider would have posted."""
    response = saml_idp.signed_response(
        idp_key, sp_entity_id=ENTITY_ID, acs_url=f"{BASE_URL}/sso/acs",
        in_response_to=request_id, **kwargs,
    )
    return client.post(
        "/sso/acs",
        data={"SAMLResponse": response, "RelayState": relay},
        follow_redirects=False,
    )


def landed_at(response) -> str:
    """Where a successful /sso/acs sends the browser next.

    Not a Location header: a successful sign-in answers with a page on our own
    origin, because a redirect out of a cross-site POST does not carry a
    SameSite=Strict cookie. See routes_sso.assertion_consumer.
    """
    assert response.status_code == 200, response.text[:300]
    match = re.search(
        r'<meta http-equiv="refresh" content="0; url=([^"]+)">', response.text
    )
    assert match, response.text[:400]
    return match.group(1)


# ---------------------------------------------------------------------------
# the exemption, and what stands in for it
# ---------------------------------------------------------------------------


def test_a_post_with_nothing_in_it_is_refused(client, idp):
    """Why the enumerating CSRF test in test_authz.py needs no exemption.

    That test posts junk to every POST route and expects 403. This route has
    no CSRF token to check, and still answers 403 -- because a request that
    carries no assertion is refused before anything else happens.
    """
    response = client.post("/sso/acs", data={"nothing": "here"},
                           follow_redirects=False)
    assert response.status_code == 403


def test_an_assertion_this_browser_did_not_ask_for_is_refused(client, idp, conn):
    """No handshake, no sign-in -- even with a perfectly valid assertion."""
    response = finish(client, idp, "_never_requested", "no-such-relay")
    assert response.status_code == 403
    assert "stockroom_session" not in response.cookies
    assert conn.execute("SELECT COUNT(*) n FROM account").fetchone()["n"] == 0


def test_an_assertion_bound_to_another_browser_is_refused(app, idp, conn):
    """Login CSRF: the attack the state cookie exists to stop.

    The attacker starts a sign-in in their own browser and gets RIT to sign an
    assertion naming *them*. They never complete it, so the handshake is still
    unspent and its InResponseTo still matches. They then get the victim's
    browser to post that assertion.

    Everything except the cookie lines up. If this test fails, the victim is
    silently signed in as the attacker and everything they do next is done in
    the attacker's account, where the attacker can read it.
    """
    with TestClient(app) as attacker, TestClient(app) as victim:
        request_id, relay = start(attacker)
        response = finish(victim, idp, request_id, relay)

    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) n FROM account").fetchone()["n"] == 0


def test_a_replayed_assertion_is_refused(client, idp, conn):
    """An assertion is good exactly once, even in the browser that earned it."""
    request_id, relay = start(client)
    first = finish(client, idp, request_id, relay)
    assert first.status_code == 200

    second = finish(client, idp, request_id, relay)
    assert second.status_code == 403
    assert conn.execute("SELECT COUNT(*) n FROM session").fetchone()["n"] == 1


def test_an_expired_handshake_is_refused(client, idp, conn):
    """Five minutes to finish signing in, not indefinitely."""
    request_id, relay = start(client)
    conn.execute(
        "UPDATE saml_auth_request SET expires_at = '2000-01-01T00:00:00Z' "
        "WHERE request_id = ?", (request_id,)
    )
    conn.commit()
    assert finish(client, idp, request_id, relay).status_code == 403


def test_a_mismatched_relay_state_is_refused(client, idp):
    request_id, _ = start(client)
    assert finish(client, idp, request_id, "not-the-relay-state").status_code == 403


def test_the_destination_never_travels_through_the_identity_provider(client, idp):
    """`next` stays on this server, so nothing comes back from outside.

    RelayState is an opaque nonce. That removes the open-redirect question
    entirely rather than answering it, and it keeps the stockroom's internal
    paths out of another organisation's logs.
    """
    response = client.get("/sso/login?next=/items", follow_redirects=False)
    location = response.headers["location"]
    assert "/items" not in location
    relay = parse_qs(urlparse(location).query)["RelayState"][0]
    assert "/items" not in relay


def test_the_return_path_still_goes_through_safe_path(client, idp, conn):
    """Even though it never left, it is checked on the way back out."""
    client.get("/sso/login?next=https://evil.invalid/steal", follow_redirects=False)
    stored = conn.execute(
        "SELECT return_to FROM saml_auth_request"
    ).fetchone()["return_to"]
    assert stored == "/"


def test_the_acs_is_still_size_capped(client, idp):
    """The exemption skips the token check and nothing else.

    The body is still read through the capped reader, which is what stops a
    chunked POST exhausting a Pi's memory.
    """
    huge = "A" * (config.MAX_UPLOAD_BYTES + 1024)
    response = client.post("/sso/acs", data={"SAMLResponse": huge},
                           follow_redirects=False)
    assert response.status_code == 413


def test_the_state_cookie_is_httponly_and_short_lived(client, idp):
    """It is cross-site readable by necessity, so it must be worth very little."""
    response = client.get("/sso/login", follow_redirects=False)
    header = response.headers["set-cookie"]
    assert "stockroom_saml" in header
    assert "HttpOnly" in header
    assert f"Max-Age={config.SSO_HANDSHAKE_TTL_SECONDS}" in header


# ---------------------------------------------------------------------------
# the assertion itself
# ---------------------------------------------------------------------------


def test_a_valid_assertion_signs_someone_in(client, idp, conn):
    request_id, relay = start(client, "/items")
    response = finish(client, idp, request_id, relay)

    assert landed_at(response).startswith("/items")
    assert "stockroom_session" in client.cookies

    account = accounts.find_by_email(conn, "abc1234@rit.edu")
    assert account is not None
    assert account.role == "requester"
    assert account.status == "active"
    assert account.auth_source == "sso"
    assert account.sso_uid == "abc1234"
    assert account.affiliation == "Student"
    assert account.password_hash == ""


@pytest.mark.parametrize(
    "label",
    ["unsigned", "signed by an impostor", "issued by somebody else", "expired"],
)
def test_an_untrustworthy_assertion_is_refused(client, idp, keypairs, conn, label):
    """Real signature validation, not a stub. A stubbed check proves nothing."""
    signer, kwargs = idp, {}
    if label == "unsigned":
        kwargs = {"sign": False}
    elif label == "signed by an impostor":
        signer = keypairs[2]
    elif label == "issued by somebody else":
        kwargs = {"issuer": "https://evil.invalid/idp"}
    elif label == "expired":
        kwargs = {"lifetime_seconds": -600}

    request_id, relay = start(client)
    assert finish(client, signer, request_id, relay, **kwargs).status_code == 403
    assert conn.execute("SELECT COUNT(*) n FROM account").fetchone()["n"] == 0


def test_a_tampered_assertion_is_refused(client, idp, conn):
    """Editing an attribute after signing invalidates the signature."""
    request_id, relay = start(client)
    good = saml_idp.signed_response(
        idp, sp_entity_id=ENTITY_ID, acs_url=f"{BASE_URL}/sso/acs",
        in_response_to=request_id,
    )
    raw = base64.b64decode(good).decode()
    forged = base64.b64encode(
        raw.replace("abc1234@rit.edu", "admin@rit.edu").encode()
    ).decode()

    response = client.post(
        "/sso/acs", data={"SAMLResponse": forged, "RelayState": relay},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) n FROM account").fetchone()["n"] == 0


def test_an_assertion_without_uid_or_mail_is_refused(client, idp):
    """A release-policy problem at RIT, not something to guess around."""
    request_id, relay = start(client)
    response = finish(client, idp, request_id, relay,
                      attributes={"sn": ["Byron"]})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def test_a_second_sign_in_reuses_the_account(client, idp, conn):
    for _ in range(2):
        # Cleared between rounds because /sso/login short-circuits for a
        # browser that is already signed in -- which is itself correct, and
        # is why this is a fresh visit rather than a second click.
        client.cookies.clear()
        request_id, relay = start(client)
        assert finish(client, idp, request_id, relay).status_code == 200
    assert conn.execute("SELECT COUNT(*) n FROM account").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM session").fetchone()["n"] == 2


def test_an_existing_requester_keeps_its_history(client, idp, conn):
    """The migration property: nobody re-registers, nobody loses anything."""
    existing = accounts.register(
        conn, first_name="Ada", last_name="Byron", email="abc1234@rit.edu",
        password="glass onion tuesday lamp", role="requester", status="active",
        actor=SETUP,
    )

    request_id, relay = start(client)
    landed_at(finish(client, idp, request_id, relay))

    linked = accounts.find_by_email(conn, "abc1234@rit.edu")
    assert linked.id == existing.id
    assert linked.sso_uid == "abc1234"
    # It still has its password: this account can use either door until the
    # stockroom decides to close one.
    assert linked.can_use_password


def test_a_staff_account_is_not_adopted_on_an_email_match(client, idp, conn):
    """The one way this flow could hand out real authority by accident.

    An email match is enough to provision, and not enough to inherit a role
    that can write equipment off: RIT reissue addresses after people leave,
    which is exactly why `uid` and not `mail` is the primary key here. So a
    staff or admin account is linked by a human, from a shell, and until then
    RIT sign-in refuses it rather than adopting it.
    """
    existing = accounts.register(
        conn, first_name="Ada", last_name="Byron", email="abc1234@rit.edu",
        password="glass onion tuesday lamp", role="staff", status="active",
        actor=SETUP,
    )

    request_id, relay = start(client)
    response = finish(client, idp, request_id, relay)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")
    assert "stockroom_session" not in response.cookies

    unchanged = accounts.get_account(conn, existing.id)
    assert unchanged.sso_uid is None
    assert unchanged.role == "staff"
    # And no shadow account was made instead.
    assert conn.execute(
        "SELECT COUNT(*) n FROM account WHERE email = ?", ("abc1234@rit.edu",)
    ).fetchone()["n"] == 1


def test_a_linked_staff_account_signs_in_and_keeps_its_role(client, idp, conn):
    """What `stockroom user link-sso` buys: the same migration, deliberately."""
    existing = accounts.register(
        conn, first_name="Ada", last_name="Byron", email="abc1234@rit.edu",
        password="glass onion tuesday lamp", role="staff", status="active",
        actor=SETUP,
    )
    accounts.link_sso(
        conn, actor=SETUP, account_id=existing.id, sso_uid="abc1234"
    )

    request_id, relay = start(client)
    landed_at(finish(client, idp, request_id, relay))

    linked = accounts.find_by_email(conn, "abc1234@rit.edu")
    assert linked.id == existing.id
    assert linked.role == "staff"
    assert linked.sso_uid == "abc1234"
    assert linked.can_use_password


def test_a_uid_cannot_be_linked_to_two_accounts(conn):
    """The unique index says so; link_sso says so first, and in English."""
    from stockroom.service import ConflictError

    first = accounts.register(
        conn, first_name="Ada", last_name="Byron", email="abc1234@rit.edu",
        password="glass onion tuesday lamp", role="staff", status="active",
        actor=SETUP,
    )
    second = accounts.register(
        conn, first_name="Grace", last_name="Hopper", email="ghz9999@rit.edu",
        password="glass onion tuesday lamp", role="staff", status="active",
        actor=SETUP,
    )
    accounts.link_sso(conn, actor=SETUP, account_id=first.id, sso_uid="abc1234")

    with pytest.raises(ConflictError):
        accounts.link_sso(
            conn, actor=SETUP, account_id=second.id, sso_uid="abc1234"
        )
    with pytest.raises(ConflictError):
        accounts.link_sso(
            conn, actor=SETUP, account_id=first.id, sso_uid="somebodyelse"
        )


def test_an_sso_account_cannot_be_signed_into_with_a_password(client, idp, conn):
    request_id, relay = start(client)
    finish(client, idp, request_id, relay)

    for attempt in ("", "anything at all", "glass onion tuesday lamp"):
        with pytest.raises(accounts.AuthError):
            accounts.login(conn, email="abc1234@rit.edu", password=attempt)


def test_a_disabled_account_is_refused_over_sso(client, idp, conn, monkeypatch):
    admin = accounts.register(
        conn, first_name="Root", last_name="Admin", email="root@rit.edu",
        password="glass onion tuesday lamp", role="admin", status="active",
        actor=SETUP,
    )
    request_id, relay = start(client)
    finish(client, idp, request_id, relay)
    account = accounts.find_by_email(conn, "abc1234@rit.edu")
    accounts.set_status(conn, actor=admin.as_actor(), account_id=account.id,
                        status="disabled")

    client.cookies.clear()
    request_id, relay = start(client)
    response = finish(client, idp, request_id, relay)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")
    assert "stockroom_session" not in response.cookies


def test_an_account_awaiting_approval_survives_being_refused(client, idp, conn,
                                                             monkeypatch):
    """Provisioning must not be rolled back by the refusal that follows it.

    With auto-approval off, a first sign-in creates a pending account and then
    refuses it. If that happened in one transaction the account would vanish,
    staff would have nothing to approve, and the person could do nothing but
    try again forever.
    """
    monkeypatch.setattr(config, "SSO_AUTO_APPROVE", False)
    request_id, relay = start(client)
    response = finish(client, idp, request_id, relay)

    assert response.status_code == 303
    assert "stockroom_session" not in response.cookies
    pending = accounts.find_by_email(conn, "abc1234@rit.edu")
    assert pending is not None and pending.status == "pending"


def test_signing_in_is_audited(client, idp, conn):
    request_id, relay = start(client)
    finish(client, idp, request_id, relay)
    actions = [r["action"] for r in conn.execute("SELECT action FROM event")]
    assert "account.sso_provision" in actions
    assert "auth.sso_login" in actions


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def test_the_default_mode_changes_nothing(client, temp_env):
    """Password mode is the default and must behave exactly as it always did."""
    assert config.AUTH_MODE == "password"
    assert "Sign in" in client.get("/login").text
    assert 'name="password"' in client.get("/login").text

    response = client.get("/sso/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")


def test_sso_mode_sends_anonymous_visitors_to_rit(client, idp):
    """The deny-by-default gate follows the mode through one seam: login_url."""
    response = client.get("/items", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/sso/login?next=/items"


def test_both_mode_offers_both(client, idp, monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "both")
    body = client.get("/login").text
    assert "/sso/login" in body
    assert 'name="password"' in body


def test_signing_out_does_not_sign_you_straight_back_in(client, idp, conn):
    """Under SSO, /login would bounce off RIT and back in. This is the fix."""
    request_id, relay = start(client)
    finish(client, idp, request_id, relay)

    token = re.search(r'name="_csrf" value="([^"]+)"', client.get("/").text).group(1)
    response = client.post("/logout", data={"_csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/sso/signed-out")

    landing = client.get("/sso/signed-out", follow_redirects=False)
    assert landing.status_code == 200
    assert "still signed in to RIT" in landing.text


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_the_metadata_names_us_and_offers_no_logout(client, idp):
    """ITS fetch this to register us. It must match what we actually do."""
    response = client.get("/sso/metadata")
    assert response.status_code == 200
    body = response.text
    assert ENTITY_ID in body
    assert f"{BASE_URL}/sso/acs" in body
    # RIT's identity provider publishes no SingleLogoutService, so neither do
    # we. Advertising an endpoint that does not work is worse than none.
    assert "SingleLogoutService" not in body


def test_the_metadata_says_so_when_nothing_is_configured(client, temp_env):
    response = client.get("/sso/metadata")
    assert response.status_code == 503
    assert "not configured" in response.text


def test_the_metadata_offers_an_encryption_key_as_well_as_a_signing_one(idp):
    """What ITS register decides whether RIT can ever encrypt to us.

    A Shibboleth identity provider encrypts to the service providers that
    advertise a key for it. Publishing only a signing key says "we cannot
    decrypt anything", and fixing that later is a second ticket -- so the
    document offers both, and offers them while
    STOCKROOM_SSO_ENCRYPTED_ASSERTIONS is off, which is the whole point.

    RIT ask for this twice over: the service provider page configures
    `encryption="true"`, and the metadata template on the ITS request form
    carries both KeyDescriptors.
    """
    assert config.SSO_ENCRYPTED_ASSERTIONS is False, "the default under test"
    body = saml.sp_metadata()
    assert 'use="signing"' in body
    assert 'use="encryption"' in body


def test_offering_the_key_does_not_start_refusing_cleartext(client, idp, conn):
    """The metadata override must not leak into what we accept.

    Advertising an encryption key and requiring encryption are different
    settings that the toolkit conflates, so the risk in fixing one is silently
    changing the other -- and the failure would be every sign-in refused the
    day this shipped, because RIT are not encrypting yet.
    """
    saml.sp_metadata()                      # the call that does the override
    assert saml.settings_dict()["security"]["wantAssertionsEncrypted"] is False

    request_id, relay = start(client)
    assert landed_at(finish(client, idp, request_id, relay)).startswith("/")


def test_the_metadata_never_expires(idp):
    """The document ITS register is a static file, and must not have a date.

    python3-saml stamps validUntil two days out by default. The ITS request
    form says to attach the metadata when they cannot fetch the URL, and this
    host is firewalled to campus, so attaching it is the expected case -- and
    a document that expires on Thursday takes every sign-in with it, with an
    error at the far end that says nothing about a date. RIT's own template
    carries no validUntil either.
    """
    body = saml.sp_metadata()
    assert "validUntil" not in body
    # cacheDuration stays: a provider that CAN fetch the URL still refreshes.
    assert "cacheDuration" in body


def test_the_metadata_is_rate_limited(client, idp, monkeypatch):
    """Public, unauthenticated, and not free to answer."""
    monkeypatch.setattr(
        "stockroom.web.routes_sso._metadata_throttle",
        security.RateLimiter(limit=2, per_seconds=60),
    )
    assert client.get("/sso/metadata").status_code == 200
    assert client.get("/sso/metadata").status_code == 200
    assert client.get("/sso/metadata").status_code == 429


# ---------------------------------------------------------------------------
# algorithms
# ---------------------------------------------------------------------------


def test_an_assertion_signed_with_sha1_is_refused_by_default(client, idp, conn):
    """SHA-1 is not a signature, and the default says so."""
    request_id, relay = start(client)
    response = finish(client, idp, request_id, relay, digest="sha1")
    assert response.status_code == 403
    assert accounts.find_by_email(conn, "abc1234@rit.edu") is None


def test_sha1_can_be_accepted_when_rits_identity_provider_needs_it(
        client, idp, conn, monkeypatch):
    """The escape hatch, proved to work.

    RIT's IdP metadata carries a certificate issued in 2008 and still
    advertises SAML 1.1, so it may sign the way an identity provider of that
    age signed. If it does, the alternative to this switch is nobody signing
    in until ITS change something -- see config.SSO_REJECT_SHA1, which is
    equally plain that using it is a weakening.
    """
    monkeypatch.setattr(config, "SSO_REJECT_SHA1", False)
    request_id, relay = start(client)
    landed_at(finish(client, idp, request_id, relay, digest="sha1"))
    assert accounts.find_by_email(conn, "abc1234@rit.edu") is not None


def test_the_doctor_says_so_while_sha1_is_being_accepted(idp, conn, monkeypatch):
    """A stopgap nobody is reminded of becomes a permanent setting."""
    from stockroom import diagnostics

    assert diagnostics.check_sso(conn).status == diagnostics.OK

    monkeypatch.setattr(config, "SSO_REJECT_SHA1", False)
    check = diagnostics.check_sso(conn)
    assert check.status == diagnostics.WARN
    assert "SHA-1" in check.detail


def test_the_authn_request_is_signed(client, idp):
    """RIT configure `signing="true"` and their metadata template agrees."""
    response = client.get("/sso/login", follow_redirects=False)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert "Signature" in query
    assert query["SigAlg"] == [
        "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
    ]
    assert 'AuthnRequestsSigned="true"' in saml.sp_metadata()


# ---------------------------------------------------------------------------
# the settings cache
# ---------------------------------------------------------------------------


def test_the_identity_providers_metadata_is_parsed_once_not_per_request(idp):
    """/sso/login, /sso/acs and /sso/metadata all used to re-parse it.

    The parse is lxml over RIT's document, and /sso/metadata is public. What
    is cached is only the parsing; the settings dict is rebuilt every call
    because the toolkit writes its defaults into whatever it is handed.
    """
    reads = []
    original = saml._read
    saml._MATERIAL = None
    try:
        saml._read = lambda path: (reads.append(path), original(path))[1]
        first = saml.settings_dict()
        second = saml.settings_dict()
        saml.settings_dict()
    finally:
        saml._read = original
        saml._MATERIAL = None

    # Three files, read once between them -- not once per call.
    assert len(reads) == 3, reads

    assert first == second
    assert first is not second, "a shared dict would collect the toolkit's defaults"
    assert first["idp"] is not second["idp"], "nor would a shared idp dict"


def test_refreshing_rits_metadata_takes_effect_without_a_restart(idp, tmp_path):
    """`stockroom sso init --refresh` rewrites the file the cache is keyed on."""
    before = saml.settings_dict()["idp"]["entityId"]
    assert before == saml_idp.IDP_ENTITY_ID

    replacement = config.SSO_IDP_METADATA.read_text().replace(
        saml_idp.IDP_ENTITY_ID, "https://elsewhere.invalid/idp/shibboleth"
    )
    config.SSO_IDP_METADATA.write_text(replacement)
    # st_mtime_ns has nanosecond resolution, but the size changed too and the
    # key carries both -- so this does not depend on the clock ticking.
    assert saml.settings_dict()["idp"]["entityId"] == (
        "https://elsewhere.invalid/idp/shibboleth"
    )


def test_the_public_paths_do_not_match_a_longer_word():
    """The same trap /public has: a prefix would exempt anything beginning so."""
    assert deps.is_public_path("/sso/login")
    assert deps.is_public_path("/sso/acs")
    assert not deps.is_public_path("/sso/admin")
    assert not deps.is_public_path("/ssofoo")


# ---------------------------------------------------------------------------
# getting the session cookie home
# ---------------------------------------------------------------------------


def test_a_successful_sign_in_answers_with_a_page_not_a_redirect(client, idp):
    """The SameSite=Strict problem, and the shape of the fix.

    A Strict cookie is withheld from any navigation a cross-site page
    initiated, including every hop of a redirect chain that began cross-site
    -- which is what RIT's form POST to /sso/acs is. Redirecting from here
    therefore delivered the browser to a page WITHOUT the session cookie just
    set: a loop under AUTH_MODE="sso", and the password form under "both".

    Serving a page on our own origin ends the cross-site chain, so the meta
    refresh out of it is same-site and carries the cookie. No test client
    implements SameSite, so what is asserted here is the mechanism: a 200 from
    our origin carrying the cookie, and no Location for a browser to follow
    while still inside RIT's navigation.
    """
    request_id, relay = start(client, "/items")
    response = finish(client, idp, request_id, relay)

    assert response.status_code == 200
    assert "location" not in response.headers
    assert any(
        "stockroom_session=" in header
        for header in response.headers.get_list("set-cookie")
    )
    assert landed_at(response).startswith("/items")


def test_the_landing_page_carries_the_flash_a_redirect_would_have(client, idp):
    """Signing in still says so, even though nothing redirects any more."""
    request_id, relay = start(client)
    target = landed_at(finish(client, idp, request_id, relay))
    assert "ok=Signed%20in%20as%20Ada%20Byron." in target


def test_the_landing_page_needs_no_javascript(client, idp):
    """The CSP has no 'unsafe-inline' and the browser has not seen our nonce.

    A sign-in that completes only with JavaScript enabled is a sign-in that
    fails silently without it, on the one page where the person cannot tell
    what went wrong.
    """
    request_id, relay = start(client)
    body = finish(client, idp, request_id, relay).text
    assert "<script" not in body
    assert 'http-equiv="refresh"' in body


def test_the_landing_target_still_goes_through_safe_path(client, idp):
    """It is a URL handed to a browser, so it is a redirect in all but name."""
    request_id, relay = start(client, "https://evil.invalid/steal")
    assert landed_at(finish(client, idp, request_id, relay)).startswith("/?")


# ---------------------------------------------------------------------------
# how RIT might spell an attribute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("uid_name, mail_name", [
    ("uid", "mail"),
    ("urn:oid:0.9.2342.19200300.100.1.1", "urn:oid:0.9.2342.19200300.100.1.3"),
    ("0.9.2342.19200300.100.1.1", "0.9.2342.19200300.100.1.3"),
    ("urn:mace:dir:attribute-def:uid", "urn:mace:dir:attribute-def:mail"),
])
def test_every_spelling_rit_might_use_is_read(client, idp, conn, uid_name,
                                              mail_name):
    """The toolkit keys attributes by `Name`, so a spelling we do not list is
    silently dropped -- and a dropped uid or mail is a refused sign-in.

    The bare OID is the form RIT's own cookbook uses; the urn:mace form is the
    one a SAML 1.1-era release policy carries, and rit-metadata.xml still
    advertises SAML 1.1.
    """
    request_id, relay = start(client)
    landed_at(finish(client, idp, request_id, relay, attributes={
        uid_name: ["abc1234"],
        mail_name: ["abc1234@rit.edu"],
    }))
    account = accounts.find_by_email(conn, "abc1234@rit.edu")
    assert account is not None and account.sso_uid == "abc1234"


def test_an_attribute_we_cannot_place_is_logged_not_swallowed(client, idp, caplog):
    """Otherwise a mis-spelled release is indistinguishable from no release.

    Names only. The values are personal data and are never logged.
    """
    request_id, relay = start(client)
    with caplog.at_level("INFO", logger="stockroom.saml"):
        finish(client, idp, request_id, relay, attributes={
            "uid": ["abc1234"],
            "mail": ["abc1234@rit.edu"],
            "ritEduSomethingNew": ["a value nobody should log"],
        })
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "ritEduSomethingNew" in messages
    assert "a value nobody should log" not in messages
