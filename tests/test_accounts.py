"""The account lifecycle: registration, approval, login, sessions."""

import pytest

from stockroom import accounts, db, security, service
from stockroom.service import Actor, ConflictError, ValidationError

SETUP = Actor("cli:test")
STRONG = "glass onion tuesday lamp"
OTHER = "seventeen purple bicycles"


@pytest.fixture
def admin(conn):
    return accounts.register(
        conn, first_name="Carter", last_name="Laubach", email="carter@rit.edu",
        password=STRONG, role="admin", status="active", actor=SETUP,
    )


@pytest.fixture
def pending(conn):
    return accounts.register(
        conn, first_name="Alice", last_name="Nguyen", email="an1234@rit.edu",
        password=OTHER, actor=SETUP,
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_registration_defaults_to_pending_and_lowest_role(pending):
    assert pending.status == "pending"
    assert pending.role == "requester"
    assert pending.name == "Alice Nguyen"


def test_a_non_rit_address_is_refused(conn):
    with pytest.raises(ValidationError, match="rit.edu"):
        accounts.register(conn, first_name="E", last_name="V",
                          email="evil@gmail.com", password=STRONG, actor=SETUP)


def test_a_weak_password_is_refused(conn):
    with pytest.raises(security.PasswordError):
        accounts.register(conn, first_name="A", last_name="B",
                          email="ab1234@rit.edu", password="password123", actor=SETUP)


def test_the_password_is_never_stored_in_the_clear(conn, pending):
    row = conn.execute("SELECT password_hash FROM account WHERE id = ?",
                       (pending.id,)).fetchone()
    assert OTHER not in row["password_hash"]
    assert row["password_hash"].startswith("scrypt$")


def test_a_duplicate_email_is_refused(conn, pending):
    with pytest.raises(ConflictError):
        accounts.register(conn, first_name="Someone", last_name="Else",
                          email="AN1234@RIT.EDU", password=STRONG, actor=SETUP)


def test_registration_is_audited(conn, pending):
    actions = [e.action for e in service.list_events(conn)]
    assert "account.register" in actions


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def test_a_pending_account_cannot_sign_in(conn, pending):
    with pytest.raises(accounts.AuthError):
        accounts.login(conn, email="an1234@rit.edu", password=OTHER)


def test_approval_activates_and_links_a_person(conn, admin, pending):
    approved = accounts.approve(conn, actor=admin.as_actor(),
                                account_id=pending.id, approved_by=admin)
    assert approved.status == "active"
    # A borrower record so equipment can be lent to them straight away.
    assert approved.person_id is not None
    assert service.get_person(conn, approved.person_id).email == "an1234@rit.edu"


def test_approval_reuses_an_existing_person(conn, admin):
    """Someone who has borrowed before keeps their loan history."""
    person = service.create_person(conn, actor=SETUP, name="Alice Nguyen",
                                   email="an1234@rit.edu")
    account = accounts.register(conn, first_name="Alice", last_name="Nguyen",
                                email="an1234@rit.edu", password=OTHER, actor=SETUP)
    approved = accounts.approve(conn, actor=admin.as_actor(),
                                account_id=account.id, approved_by=admin)
    assert approved.person_id == person.id


def test_approving_twice_is_refused(conn, admin, pending):
    accounts.approve(conn, actor=admin.as_actor(), account_id=pending.id,
                     approved_by=admin)
    with pytest.raises(ConflictError, match="already active"):
        accounts.approve(conn, actor=admin.as_actor(), account_id=pending.id,
                         approved_by=admin)


def test_a_cli_created_account_is_active_immediately(admin):
    """Made by someone with shell access; there is nobody else to approve it."""
    assert admin.status == "active"
    assert admin.role == "admin"
    assert admin.person_id is not None


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


@pytest.fixture
def active(conn, admin, pending):
    return accounts.approve(conn, actor=admin.as_actor(), account_id=pending.id,
                            approved_by=admin)


def test_login_succeeds_and_opens_a_session(conn, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    assert result.account.id == active.id
    resolved = accounts.resolve_session(conn, result.token)
    assert resolved is not None and resolved[1].id == active.id


def test_only_the_token_hash_reaches_the_database(conn, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    stored = conn.execute("SELECT token_hash FROM session").fetchone()["token_hash"]
    assert stored != result.token
    assert stored == security.token_hash(result.token)


def test_a_wrong_password_is_refused(conn, active):
    with pytest.raises(accounts.AuthError):
        accounts.login(conn, email="an1234@rit.edu", password="wrong wrong wrong")


def test_failures_do_not_reveal_whether_an_account_exists(conn, active):
    """Same message for a real address and an unknown one."""
    messages = set()
    for email in ("an1234@rit.edu", "nobody@rit.edu"):
        try:
            accounts.login(conn, email=email, password="definitely not it")
        except accounts.AuthError as exc:
            messages.add(str(exc))
    assert len(messages) == 1


def test_lockout_applies_equally_to_unknown_addresses(conn, active):
    """Otherwise lockout itself becomes the enumeration oracle."""

    def attempt_until_locked(email):
        for index in range(security.MAX_FAILURES_PER_EMAIL + 1):
            try:
                accounts.login(conn, email=email, password="wrong wrong wrong",
                               ip="10.0.0.1")
            except accounts.AuthError as exc:
                if "Too many" in str(exc):
                    return index
        return None

    assert attempt_until_locked("an1234@rit.edu") == \
           attempt_until_locked("ghost@rit.edu")


def test_a_correct_password_is_refused_while_locked_out(conn, active):
    for _ in range(security.MAX_FAILURES_PER_EMAIL):
        with pytest.raises(accounts.AuthError):
            accounts.login(conn, email="an1234@rit.edu", password="nope nope nope",
                           ip="10.0.0.1")
    with pytest.raises(accounts.AuthError, match="Too many"):
        accounts.login(conn, email="an1234@rit.edu", password=OTHER, ip="10.0.0.1")


def test_a_blocked_attempt_does_not_extend_the_lockout(conn, active):
    """Otherwise anyone can lock any staff account out for good.

    A rejected-while-locked attempt never reaches the password check, so there
    is nothing to count. Counting it anyway fed the sliding window that caused
    the lockout, which made it self-sustaining: five guesses every fifteen
    minutes and the real owner never gets back in.
    """
    for _ in range(security.MAX_FAILURES_PER_EMAIL):
        with pytest.raises(accounts.AuthError):
            accounts.login(conn, email="an1234@rit.edu", password="nope nope nope",
                           ip="10.0.0.1")

    def failures():
        return conn.execute(
            "SELECT COUNT(*) FROM auth_attempt WHERE email = ? AND success = 0",
            ("an1234@rit.edu",),
        ).fetchone()[0]

    recorded = failures()
    for _ in range(10):
        with pytest.raises(accounts.AuthError, match="Too many"):
            accounts.login(conn, email="an1234@rit.edu", password="nope nope nope",
                           ip="10.0.0.1")

    assert failures() == recorded, \
        "attempts refused by the lockout must not feed the lockout window"
    assert "auth.lockout" in [e.action for e in service.list_events(conn)], \
        "the blocked attempt must still be audited"


def test_a_locked_out_address_cannot_flood_the_audit_log(conn, active):
    """The lockout must not be a cheaper way to write than a real login.

    A blocked attempt never reaches scrypt and records no auth_attempt row, so
    nothing throttled it: every refusal appended a hash-chained event and took
    the database's only write lock, at thousands per second. That fills an SD
    card and contends with the counter, and it buries the inventory history
    the log exists to keep.

    One event per address per window says everything worth saying -- that this
    account was attacked, and when.
    """
    for _ in range(security.MAX_FAILURES_PER_EMAIL):
        with pytest.raises(accounts.AuthError):
            accounts.login(conn, email="an1234@rit.edu", password="nope nope nope",
                           ip="10.0.0.1")

    def lockout_events():
        return len(service.list_events(conn, action="auth.lockout", limit=1000))

    for _ in range(50):
        with pytest.raises(accounts.AuthError, match="Too many"):
            accounts.login(conn, email="an1234@rit.edu", password="nope nope nope",
                           ip="10.0.0.1")

    assert lockout_events() == 1, \
        "a blocked attempt must not append an event every time"


def test_a_second_address_still_gets_its_own_lockout_event(conn, active, admin):
    """Throttling is per address, so one victim cannot mask another."""
    for email in ("an1234@rit.edu", "carter@rit.edu"):
        for _ in range(security.MAX_FAILURES_PER_EMAIL + 2):
            with pytest.raises(accounts.AuthError):
                accounts.login(conn, email=email, password="nope nope nope",
                               ip="10.0.0.2")

    blocked = service.list_events(conn, action="auth.lockout", limit=1000)
    assert len(blocked) == 2
    summaries = " ".join(e.summary for e in blocked)
    assert "an1234@rit.edu" in summaries and "carter@rit.edu" in summaries


def test_login_upgrades_a_weakly_hashed_password(conn, admin):
    """The rehash path: the only moment the plaintext is available."""
    weak = security.hash_password(STRONG, n=2**14, r=8, p=1)
    with db.transaction(conn):
        conn.execute("UPDATE account SET password_hash = ? WHERE id = ?",
                     (weak, admin.id))
    accounts.login(conn, email="carter@rit.edu", password=STRONG)
    upgraded = accounts.get_account(conn, admin.id).password_hash
    assert upgraded.startswith(f"scrypt${security.SCRYPT_N}$")
    assert security.verify_password(STRONG, upgraded).ok


def test_login_and_failure_are_both_audited(conn, active):
    with pytest.raises(accounts.AuthError):
        accounts.login(conn, email="an1234@rit.edu", password="wrong wrong wrong")
    accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    actions = [e.action for e in service.list_events(conn)]
    assert "auth.login" in actions
    assert "auth.login_failed" in actions


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_an_expired_session_stops_resolving(conn, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    with db.transaction(conn):
        conn.execute("UPDATE session SET expires_at = '2000-01-01T00:00:00Z'")
    assert accounts.resolve_session(conn, result.token) is None


def test_the_absolute_cap_is_never_extended(conn, active):
    """Idle expiry slides; the hard cap does not, however active you are."""
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    original = conn.execute("SELECT absolute_expires_at FROM session").fetchone()[0]
    for _ in range(3):
        accounts.resolve_session(conn, result.token)
    assert conn.execute("SELECT absolute_expires_at FROM session").fetchone()[0] == original


def test_the_idle_expiry_slides_forward_on_use(conn, active, monkeypatch):
    """Nothing asserted this at all, so nothing would have noticed it stop.

    The existing cap test passes with the UPDATE deleted outright.
    """
    monkeypatch.setattr(accounts, "HEARTBEAT_SECONDS", 0)
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)

    # An idle expiry far enough out to still be live, a stale heartbeat, and
    # an absolute cap beyond both so it is not what limits the slide.
    with db.transaction(conn):
        conn.execute(
            "UPDATE session SET expires_at = ?, last_seen_at = ?, "
            "absolute_expires_at = ?",
            ("2030-01-01T00:00:00Z", "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )

    accounts.resolve_session(conn, result.token)
    row = conn.execute("SELECT expires_at, last_seen_at FROM session").fetchone()
    assert row["expires_at"] != "2030-01-01T00:00:00Z", "the idle expiry did not move"
    assert row["last_seen_at"] != "2000-01-01T00:00:00Z"


def test_the_heartbeat_does_not_write_on_every_request(conn, active):
    """resolve_session took the write lock on every authenticated request.

    db.transaction() is BEGIN IMMEDIATE, so viewing any page could block a
    checkout at the counter, and every navigation wrote to the SD card. The
    window being maintained is eight hours long; a minute of lag is free.
    """
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    before = conn.execute(
        "SELECT last_seen_at, expires_at FROM session"
    ).fetchone()

    for _ in range(20):
        assert accounts.resolve_session(conn, result.token) is not None

    after = conn.execute("SELECT last_seen_at, expires_at FROM session").fetchone()
    assert (after["last_seen_at"], after["expires_at"]) == \
           (before["last_seen_at"], before["expires_at"]), \
        "the heartbeat is still writing once per request"


def test_a_session_past_its_absolute_cap_is_dead(conn, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    with db.transaction(conn):
        conn.execute(
            "UPDATE session SET absolute_expires_at = '2000-01-01T00:00:00Z'"
        )
    assert accounts.resolve_session(conn, result.token) is None


def test_logout_revokes_only_that_session(conn, active):
    first = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    second = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    accounts.logout(conn, token=first.token)
    assert accounts.resolve_session(conn, first.token) is None
    assert accounts.resolve_session(conn, second.token) is not None


def test_disabling_an_account_revokes_every_session(conn, admin, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    accounts.set_status(conn, actor=admin.as_actor(), account_id=active.id,
                        status="disabled")
    assert accounts.resolve_session(conn, result.token) is None


def test_changing_a_password_revokes_every_session(conn, active):
    result = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    accounts.change_password(conn, actor=active.as_actor(), account_id=active.id,
                             new_password="Rochester-Fog-Kettle-9",
                             current_password=OTHER)
    assert accounts.resolve_session(conn, result.token) is None
    assert accounts.login(conn, email="an1234@rit.edu",
                          password="Rochester-Fog-Kettle-9").account.id == active.id


def test_changing_a_password_needs_the_current_one(conn, active):
    with pytest.raises(accounts.AuthError):
        accounts.change_password(conn, actor=active.as_actor(),
                                 account_id=active.id,
                                 new_password="Rochester-Fog-Kettle-9",
                                 current_password="not the right one")


def test_pruning_removes_dead_sessions_only(conn, active):
    live = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    dead = accounts.login(conn, email="an1234@rit.edu", password=OTHER)
    with db.transaction(conn):
        conn.execute(
            "UPDATE session SET absolute_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE token_hash = ?", (security.token_hash(dead.token),)
        )
    assert accounts.prune_sessions(conn) == 1
    assert accounts.resolve_session(conn, live.token) is not None


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def test_role_ranking(conn, admin, active):
    assert active.has_role("requester") and not active.has_role("staff")
    assert admin.has_role("staff") and admin.has_role("admin")
    assert admin.is_staff and admin.is_admin


def test_the_last_administrator_cannot_be_demoted(conn, admin):
    with pytest.raises(ConflictError, match="only active administrator"):
        accounts.set_role(conn, actor=admin.as_actor(), account_id=admin.id,
                          role="requester")


def test_the_last_administrator_cannot_be_switched_off(conn, admin):
    """The other way to reach an installation with no administrator.

    set_role guarded demotion from the start; set_status did not guard
    disabling, so the same end state was one POST away.
    """
    with pytest.raises(ConflictError, match="only active administrator"):
        accounts.set_status(conn, actor=admin.as_actor(), account_id=admin.id,
                            status="disabled")
    assert accounts.get_account(conn, admin.id).status == "active"


def test_an_administrator_can_be_switched_off_once_another_exists(
    conn, admin, active
):
    accounts.set_role(conn, actor=admin.as_actor(), account_id=active.id, role="admin")
    disabled = accounts.set_status(conn, actor=admin.as_actor(),
                                   account_id=admin.id, status="disabled")
    assert disabled.status == "disabled"


def test_demotion_is_allowed_once_another_admin_exists(conn, admin, active):
    accounts.set_role(conn, actor=admin.as_actor(), account_id=active.id, role="admin")
    demoted = accounts.set_role(conn, actor=admin.as_actor(), account_id=admin.id,
                                role="staff")
    assert demoted.role == "staff"


def test_role_changes_are_audited(conn, admin, active):
    accounts.set_role(conn, actor=admin.as_actor(), account_id=active.id, role="staff")
    latest = service.list_events(conn, limit=1)[0]
    assert latest.action == "account.role_change"
    assert latest.changes["role"] == {"from": "requester", "to": "staff"}


def test_declining_and_disabling_are_distinct_actions(conn, admin, active):
    """Turning down a signup and switching off a working account differ.

    Both land on `disabled`, but the history should say which happened.
    """
    never_approved = accounts.register(
        conn, first_name="Never", last_name="Approved", email="na9999@rit.edu",
        password="Rochester-Fog-Kettle-9", actor=SETUP,
    )
    accounts.set_status(conn, actor=admin.as_actor(),
                        account_id=never_approved.id, status="disabled")
    assert service.list_events(conn, limit=1)[0].action == "account.decline"

    accounts.set_status(conn, actor=admin.as_actor(), account_id=active.id,
                        status="disabled")
    assert service.list_events(conn, limit=1)[0].action == "account.disable"
