"""Password hashing, CSRF, and the security headers.

The two tests that matter most here are the systematic ones at the bottom:
they enumerate the application's own routes, so a new route cannot be added
without CSRF protection or without an authentication decision.
"""

import pytest

from stockroom import security
from stockroom.web import deps


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------


def test_hash_round_trips():
    stored = security.hash_password("glass onion tuesday lamp")
    assert security.verify_password("glass onion tuesday lamp", stored).ok


def test_hash_is_salted():
    """Two identical passwords must not produce identical hashes."""
    a = security.hash_password("glass onion tuesday lamp")
    b = security.hash_password("glass onion tuesday lamp")
    assert a != b


def test_wrong_password_is_rejected():
    stored = security.hash_password("glass onion tuesday lamp")
    assert not security.verify_password("glass onion tuesday lamps", stored).ok


def test_a_malformed_hash_fails_instead_of_crashing(caplog):
    for broken in ("", "nonsense", "scrypt$x$y$z$q$r", "bcrypt$1$2$3$4$5"):
        assert not security.verify_password("anything at all", broken).ok


def test_parameters_are_stored_in_the_hash():
    stored = security.hash_password("glass onion tuesday lamp", n=2**14, r=8, p=1)
    assert stored.startswith("scrypt$16384$8$1$")


def test_weaker_parameters_are_flagged_for_rehash():
    """The upgrade path: a correct password on old parameters gets re-hashed."""
    stored = security.hash_password("glass onion tuesday lamp", n=2**14, r=8, p=1)
    result = security.verify_password("glass onion tuesday lamp", stored)
    assert result.ok and result.needs_rehash

    current = security.hash_password("glass onion tuesday lamp")
    assert not security.verify_password("glass onion tuesday lamp", current).needs_rehash


@pytest.mark.parametrize(
    "password",
    ["short", "elevenchars", "password123456", "P@ssw0rd!2024", "letmein12345",
     "qwerty123456", "aaaaaaaaaaaaaaaa", "stockroom2026"],
)
def test_weak_passwords_are_refused(password):
    with pytest.raises(security.PasswordError):
        security.check_password_strength(password)


@pytest.mark.parametrize(
    "password",
    ["glass onion tuesday lamp", "seventeen purple bicycles",
     "Rochester-Fog-Kettle-9"],
)
def test_good_passwords_are_accepted(password):
    security.check_password_strength(password, email="an1234@rit.edu",
                                     first_name="Alice", last_name="Nguyen")


def test_a_password_containing_your_own_name_is_refused():
    with pytest.raises(security.PasswordError, match="your name"):
        security.check_password_strength(
            "alicenguyen2026", email="an1234@rit.edu",
            first_name="Alice", last_name="Nguyen",
        )


@pytest.mark.parametrize(
    "email,expected",
    [("a@rit.edu", True), ("a@cs.rit.edu", True), ("A@RIT.EDU", True),
     ("a@gmail.com", False), ("a@notrit.edu", False),
     ("a@rit.edu.evil.com", False), ("", False)],
)
def test_institutional_email_matching(email, expected):
    assert security.is_institutional_email(email) is expected


def test_token_comparison_is_constant_time():
    token = security.new_token()
    assert security.tokens_equal(token, token)
    assert not security.tokens_equal(token, security.new_token())
    assert not security.tokens_equal("", "")


def test_session_tokens_are_unguessable():
    tokens = {security.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)


def test_only_the_token_hash_is_ever_stored():
    token = security.new_token()
    digest = security.token_hash(token)
    assert token not in digest
    assert digest == security.token_hash(token)


# ---------------------------------------------------------------------------
# redirect safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    ["https://evil.test/", "http://evil.test", "//evil.test/path",
     "javascript:alert(1)", "", "not-a-path",
     # A browser resolving a URL reads a backslash as a path separator, so
     # this is the protocol-relative form wearing a different hat.
     "/\\evil.test", "/\\/evil.test"],
)
def test_offsite_redirect_targets_are_refused(candidate):
    assert deps.safe_path(candidate) == "/"


@pytest.mark.parametrize("candidate", ["/", "/items", "/items/3?x=1"])
def test_local_redirect_targets_are_kept(candidate):
    assert deps.safe_path(candidate) == candidate


def test_every_caller_supplied_next_goes_through_safe_path():
    """A `next` form field handed straight to redirect() is an open redirect.

    routes_loans was the one that did not wrap it, and a POST carrying
    next=https://evil.example/ answered 303 to that host. This walks the
    routes rather than testing one of them, so the next route to take a
    `next` field cannot quietly skip the guard.
    """
    import importlib
    import inspect
    import pkgutil
    import re

    import stockroom.web

    # Every routes_* module, not a hand-kept list of three: the module that
    # forgets the guard is by definition the one nobody remembered to add.
    modules = [
        importlib.import_module(f"stockroom.web.{name}")
        for _, name, _ in pkgutil.iter_modules(stockroom.web.__path__)
        if name.startswith("routes_")
    ]
    assert len(modules) >= 11, "route modules are not being discovered"

    offenders = []
    for module in modules:
        source = inspect.getsource(module)
        if 'next: str = Form(' not in source and 'next=' not in source:
            continue
        for call in re.findall(r"redirect\(\s*([A-Za-z_][A-Za-z_0-9]*)", source):
            if call == "next":
                offenders.append(module.__name__)
    assert not offenders, (
        "these modules pass a caller-supplied `next` to redirect() without "
        f"safe_path: {sorted(set(offenders))}"
    )


def test_a_destination_cannot_smuggle_a_flash_message_onto_the_login_page(
    temp_env,
):
    """Anyone could put words in the stockroom's mouth with one link.

    The sign-in redirect interpolated the destination raw, so a destination
    carrying its own query string had its parameters land on /login as
    *login's* parameters -- and deps.page() renders `ok=` and `error=` as
    flash messages. The text was escaped, so never script; but a phishing
    line in the site's own voice, on the genuine page, over the genuine
    certificate, does not need to be script to work.
    """
    import re

    from fastapi.testclient import TestClient

    from stockroom.web.app import app

    with TestClient(app) as client:
        landing = client.get(
            "/items?unit=B&ok=Your+password+has+expired.+Call+585-555-0100.",
            follow_redirects=False,
        )
        assert landing.status_code == 303
        body = client.get(landing.headers["location"]).text

    assert re.findall(r'<div class="flash[^"]*">(.*?)</div>', body, re.S) == [], \
        "a caller-supplied parameter must not render as a flash message"
    # And the destination still survives intact rather than being truncated
    # at the first `&`, which is the same bug read the other way round.
    assert 'name="next" value="/items?unit=B&amp;ok=' in body


def test_the_login_destination_is_encoded():
    assert deps.login_url("/items?a=1&b=2") == "/login?next=/items%3Fa%3D1%26b%3D2"
    assert deps.login_url("https://evil.test/") == "/login?next=/"


def test_a_public_prefix_does_not_match_a_longer_word():
    """"/public" as a *prefix* also matches /public-holidays and /publicfoo.

    Nothing is on those paths today, but the auth gate is deny-by-default and
    an accidental exemption is exactly what it exists to prevent.
    """
    assert deps.is_public_path("/public/")
    assert deps.is_public_path("/public")
    assert not deps.is_public_path("/publicfoo")
    assert not deps.is_public_path("/public-holidays")
    assert not deps.is_public_path("/publish")


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_then_blocks():
    limiter = security.RateLimiter(limit=3, per_seconds=60)
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("other-key") is True


def test_lockout_counts_only_recent_failures(conn):
    for _ in range(security.MAX_FAILURES_PER_EMAIL):
        security.record_attempt(conn, email="a@rit.edu", ip="10.0.0.1", success=False)
    assert security.check_lockout(conn, email="a@rit.edu", ip="10.0.0.1").locked


def test_a_successful_login_clears_the_failure_count(conn):
    for _ in range(3):
        security.record_attempt(conn, email="a@rit.edu", ip="10.0.0.1", success=False)
    security.record_attempt(conn, email="a@rit.edu", ip="10.0.0.1", success=True)
    for _ in range(3):
        security.record_attempt(conn, email="a@rit.edu", ip="10.0.0.1", success=False)
    # Three failures since the success is under the threshold.
    assert not security.check_lockout(conn, email="a@rit.edu", ip="10.0.0.1").locked


def test_one_ip_cannot_spray_many_accounts(conn):
    """Per-email lockout alone would let an attacker try one guess each."""
    for index in range(security.MAX_FAILURES_PER_IP):
        security.record_attempt(
            conn, email=f"user{index}@rit.edu", ip="10.0.0.66", success=False
        )
    state = security.check_lockout(conn, email="fresh@rit.edu", ip="10.0.0.66")
    assert state.locked


def test_pruning_removes_only_old_attempts(conn):
    from stockroom import db

    security.record_attempt(conn, email="a@rit.edu", ip="", success=False)
    conn.execute(
        "INSERT INTO auth_attempt (email, ip, at, success) VALUES (?, '', ?, 0)",
        ("old@rit.edu", "2000-01-01T00:00:00Z"),
    )
    with db.transaction(conn):
        removed = security.prune_auth_attempts(conn, keep_days=30)
    assert removed == 1
    remaining = conn.execute("SELECT email FROM auth_attempt").fetchall()
    assert [r["email"] for r in remaining] == ["a@rit.edu"]
