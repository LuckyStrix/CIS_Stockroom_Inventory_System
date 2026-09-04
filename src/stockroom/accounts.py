"""Accounts, sessions and login.

    ======================================================================
    THE RULE, again: every mutation here writes its `event` row in the same
    transaction as the change. This module is a sibling of service.py, not
    an exception to it.
    ======================================================================

The one deliberate carve-out is the session heartbeat -- `last_seen_at` and
the sliding idle expiry are touched on every authenticated request, and
logging them would bury the inventory history under noise. Nothing else here
is exempt: registration, approval, login, logout, role changes and password
changes are all audited.

Design notes
------------
* **Accounts are separate from people.** `person` is anyone who can hold
  equipment, including visitors who will never log in. `account` is a
  credential. Approval links the two, by email.

* **Registration cannot grant access.** A new account is `pending` and cannot
  log in until staff approve it. There is no email server on the Pi, so staff
  approval *is* the verification step -- see docs/security.md, which says so
  rather than implying the address was proven.

* **No user enumeration.** Registration and login return the same generic
  outcome whether or not an address is already known, and the unknown-account
  login path burns the same CPU as a real one (`security.dummy_verify`).

* **Bootstrap is CLI-only.** `stockroom user create --admin` is the only way
  to make the first administrator, so there is never an unauthenticated route
  to privilege over the network.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, security
from .service import (
    Actor,
    ConflictError,
    NotFound,
    ValidationError,
    _clean,
    _require,
    find_person_by_email,
    log_event,
)

# Session lifetimes. Idle expiry slides forward on use; the absolute cap never
# does, so a session cannot live forever just because someone keeps a tab open.
IDLE_TIMEOUT_HOURS = config.SESSION_IDLE_HOURS
ABSOLUTE_TIMEOUT_DAYS = config.SESSION_MAX_DAYS

# How stale the heartbeat may get before resolve_session writes it back.
#
# That write is not free: db.transaction() is BEGIN IMMEDIATE, so it takes the
# database's single write lock. Doing it on every authenticated request meant
# that loading any page could block a checkout at the counter for up to
# busy_timeout, and put a write on the SD card for every navigation.
#
# The window it maintains is eight hours. Letting the stored value lag by a
# minute costs nothing anybody can perceive and removes the write from all but
# one request a minute per session.
HEARTBEAT_SECONDS = 60

ROLES = ("requester", "staff", "admin")
STATUSES = ("pending", "active", "disabled")

# Which door an account came in through. schema.sql carries this as a CHECK
# for a fresh database, and cannot for an upgrading one -- ALTER TABLE cannot
# add a table-level constraint -- so, exactly as loan.unit_id does, the rule
# is also enforced here in code. See CLAUDE.md.
AUTH_SOURCES = ("password", "sso")

# Ranking for permission checks: a role satisfies a requirement if it ranks at
# or above it. Keeps guards readable ("staff or better") and avoids scattering
# set membership tests through the routes.
_ROLE_RANK = {"requester": 0, "staff": 1, "admin": 2}


class AuthError(Exception):
    """Login refused. The message is deliberately vague and user-safe."""


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    id: int
    first_name: str
    last_name: str
    email: str
    password_hash: str
    role: str
    status: str
    person_id: int | None
    created_at: str
    updated_at: str
    approved_at: str | None
    approved_by_id: int | None
    last_login_at: str | None
    password_changed_at: str
    # Defaulted so that a row read from a database that predates the SSO
    # columns still builds. from_row only passes the keys the row actually
    # has, and it filters on __slots__ -- so a column missing from this
    # dataclass is silently dropped, and a dataclass field missing from the
    # row is a TypeError without these.
    sso_uid: str | None = None
    auth_source: str = "password"
    affiliation: str = ""
    last_sso_login_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Account:
        known = cls.__slots__
        return cls(**{k: row[k] for k in row.keys() if k in known})

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def has_role(self, minimum: str) -> bool:
        """Whether this account ranks at or above ``minimum``."""
        return _ROLE_RANK.get(self.role, -1) >= _ROLE_RANK[minimum]

    @property
    def is_staff(self) -> bool:
        return self.has_role("staff")

    @property
    def is_admin(self) -> bool:
        return self.has_role("admin")

    @property
    def can_use_password(self) -> bool:
        """Whether the password form could ever sign this account in.

        An SSO-provisioned account stores an empty hash, which no password
        verifies against. Templates ask this rather than reading the hash, so
        that "change your password" is not offered to someone who has none --
        a control that refuses is a bug, not a defence.
        """
        return bool(self.password_hash)

    def as_actor(self) -> Actor:
        """The audit-log identity for this account."""
        return Actor(name=self.name, email=self.email)


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    account_id: int
    token_hash: str
    csrf_token: str
    created_at: str
    last_seen_at: str
    expires_at: str
    absolute_expires_at: str
    ip: str
    user_agent: str
    revoked_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> Session:
        known = cls.__slots__
        return cls(**{k: row[k] for k in row.keys() if k in known})


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_account(conn: sqlite3.Connection, account_id: int) -> Account:
    row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound(f"No account with id {account_id}.")
    return Account.from_row(row)


def find_by_email(conn: sqlite3.Connection, email: str) -> Account | None:
    row = conn.execute(
        "SELECT * FROM account WHERE email = ? COLLATE NOCASE",
        (security.normalize_email(email),),
    ).fetchone()
    return Account.from_row(row) if row else None


def list_accounts(
    conn: sqlite3.Connection, *, status: str | None = None, role: str | None = None
) -> list[Account]:
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if role:
        where.append("role = ?")
        params.append(role)
    sql = "SELECT * FROM account"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Pending first -- that is the list staff actually need to act on.
    sql += " ORDER BY (status = 'pending') DESC, last_name COLLATE NOCASE"
    return [Account.from_row(r) for r in conn.execute(sql, params)]


def count_pending(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM account WHERE status = 'pending'"
        ).fetchone()["n"]
    )


# ---------------------------------------------------------------------------
# registration and approval
# ---------------------------------------------------------------------------


def register(
    conn: sqlite3.Connection,
    *,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    role: str = "requester",
    status: str = "pending",
    actor: Actor | None = None,
) -> Account:
    """Create an account.

    Defaults to a `pending` account with the lowest role -- the self-service
    signup path. The CLI passes ``role``/``status`` explicitly to bootstrap an
    administrator.

    Raises :class:`ConflictError` if the address is taken. Callers on the
    public signup path must **not** surface that distinction to the browser;
    see `routes_auth.register`, which reports success either way.
    """
    first_name = _require(first_name, "First name")
    last_name = _require(last_name, "Last name")
    email = security.normalize_email(_require(email, "Email"))

    if not security.is_institutional_email(email):
        raise ValidationError("Please use your RIT email address (…@rit.edu).")
    if role not in ROLES:
        raise ValidationError(f"Unknown role {role!r}.")
    if status not in STATUSES:
        raise ValidationError(f"Unknown status {status!r}.")

    security.check_password_strength(
        password, email=email, first_name=first_name, last_name=last_name
    )
    # Hashing is deliberately expensive, so do it before taking the write lock.
    password_hash = security.hash_password(password)

    with db.transaction(conn):
        if find_by_email(conn, email) is not None:
            raise ConflictError("An account with that email already exists.")

        now = db.utcnow()
        cur = conn.execute(
            """
            INSERT INTO account (first_name, last_name, email, password_hash,
                                 role, status, created_at, updated_at,
                                 password_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (first_name, last_name, email, password_hash, role, status, now, now, now),
        )
        account_id = int(cur.lastrowid)
        log_event(
            conn,
            actor=actor or Actor(name=f"{first_name} {last_name}", email=email),
            action="account.register",
            entity_type="account",
            entity_id=account_id,
            summary=f"Registered account {first_name} {last_name} <{email}> ({status})",
            changes={"role": {"from": None, "to": role},
                     "status": {"from": None, "to": status}},
        )
        account = get_account(conn, account_id)

    if account.status == "active":
        _link_person(conn, actor=actor or account.as_actor(), account=account)
        # Re-read: _link_person set person_id, and the object above predates it.
        account = get_account(conn, account.id)
    return account


def approve(
    conn: sqlite3.Connection, *, actor: Actor, account_id: int, approved_by: Account
) -> Account:
    """Activate a pending account and link it to its borrower record."""
    with db.transaction(conn):
        account = get_account(conn, account_id)
        if account.status == "active":
            raise ConflictError(f"{account.name} is already active.")

        now = db.utcnow()
        conn.execute(
            "UPDATE account SET status = 'active', approved_at = ?, "
            "approved_by_id = ?, updated_at = ? WHERE id = ?",
            (now, approved_by.id, now, account_id),
        )
        log_event(
            conn,
            actor=actor,
            action="account.approve",
            entity_type="account",
            entity_id=account_id,
            summary=f"Approved account {account.name} <{account.email}>",
            changes={"status": {"from": account.status, "to": "active"}},
        )
        account = get_account(conn, account_id)

    _link_person(conn, actor=actor, account=account)
    return get_account(conn, account_id)


def _link_person(conn: sqlite3.Connection, *, actor: Actor, account: Account) -> None:
    """Attach the account to its `person` record, creating one if needed.

    Runs after approval so that an account holder can immediately be lent
    equipment. Joined by email, which is unique on both tables.
    """
    from .service import create_person

    if account.person_id is not None:
        return
    person = find_person_by_email(conn, account.email)
    if person is None:
        person = create_person(
            conn, actor=actor, name=account.name, email=account.email
        )
    with db.transaction(conn):
        conn.execute(
            "UPDATE account SET person_id = ?, updated_at = ? WHERE id = ?",
            (person.id, db.utcnow(), account.id),
        )


def _other_active_admins(conn: sqlite3.Connection, account_id: int) -> int:
    """How many active administrators there would be without this account.

    Both set_role and set_status consult this: demoting the last admin and
    switching the last admin off are two routes to an installation nobody can
    administer, and only the first one used to be guarded.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM account "
            "WHERE role = 'admin' AND status = 'active' AND id <> ?",
            (account_id,),
        ).fetchone()["n"]
    )


def set_status(
    conn: sqlite3.Connection, *, actor: Actor, account_id: int, status: str,
    reason: str = "",
) -> Account:
    """Decline, disable or re-enable an account.

    Disabling revokes every live session for that account immediately -- an
    account that is switched off must stop working now, not in eight hours.

    Who is *allowed* to call this is decided at the route layer, which knows
    the caller's role; an Actor is a name and an email and has none. What is
    enforced here is the invariant, exactly as in set_role: the installation
    may not be left with no administrator.
    """
    if status not in STATUSES:
        raise ValidationError(f"Unknown status {status!r}.")

    with db.transaction(conn):
        account = get_account(conn, account_id)
        if account.status == status:
            return account

        # The same guard set_role has, for the other way of reaching the same
        # place. Demoting the last admin was refused; switching them off was
        # not, and it leaves an installation that cannot grant the role back
        # to anyone without shell access to the Pi.
        if account.role == "admin" and status != "active":
            if _other_active_admins(conn, account_id) == 0:
                raise ConflictError(
                    "This is the only active administrator. Promote someone "
                    "else before switching this account off."
                )
        conn.execute(
            "UPDATE account SET status = ?, updated_at = ? WHERE id = ?",
            (status, db.utcnow(), account_id),
        )
        if status != "active":
            _revoke_all_sessions(conn, account_id)
        summary = f"Set {account.name} to {status}"
        if _clean(reason):
            summary += f" ({_clean(reason)})"
        # Declining a signup and switching off a working account are different
        # events, even though both land on `disabled`.
        if status == "disabled":
            action = "account.decline" if account.status == "pending" else "account.disable"
        else:
            action = "account.status_change"
        log_event(
            conn,
            actor=actor,
            action=action,
            entity_type="account",
            entity_id=account_id,
            summary=summary,
            changes={"status": {"from": account.status, "to": status}},
        )
        return get_account(conn, account_id)


def set_role(
    conn: sqlite3.Connection, *, actor: Actor, account_id: int, role: str
) -> Account:
    """Change an account's role. Admin-only at the route layer."""
    if role not in ROLES:
        raise ValidationError(f"Unknown role {role!r}.")

    with db.transaction(conn):
        account = get_account(conn, account_id)
        if account.role == role:
            return account

        # Refuse to remove the last administrator: an installation with no
        # admin cannot grant anyone the role back without CLI access.
        if account.role == "admin" and role != "admin":
            if _other_active_admins(conn, account_id) == 0:
                raise ConflictError(
                    "This is the only active administrator. Promote someone "
                    "else before changing this account."
                )

        conn.execute(
            "UPDATE account SET role = ?, updated_at = ? WHERE id = ?",
            (role, db.utcnow(), account_id),
        )
        log_event(
            conn,
            actor=actor,
            action="account.role_change",
            entity_type="account",
            entity_id=account_id,
            summary=f"Changed {account.name}'s role from {account.role} to {role}",
            changes={"role": {"from": account.role, "to": role}},
        )
        return get_account(conn, account_id)


def change_password(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    account_id: int,
    new_password: str,
    current_password: str | None = None,
    revoke_other_sessions: bool = True,
) -> Account:
    """Set a new password.

    ``current_password`` is required when someone changes their own; an admin
    resetting another account passes None. Every other session is revoked by
    default, because a password change is usually a response to suspecting one
    has been stolen.
    """
    account = get_account(conn, account_id)

    if not account.can_use_password:
        raise ConflictError(
            "This account signs in with RIT single sign-on and has no password."
        )

    if current_password is not None:
        if not security.verify_password(current_password, account.password_hash).ok:
            raise AuthError("Current password is incorrect.")

    security.check_password_strength(
        new_password, email=account.email,
        first_name=account.first_name, last_name=account.last_name,
    )
    password_hash = security.hash_password(new_password)

    with db.transaction(conn):
        now = db.utcnow()
        conn.execute(
            "UPDATE account SET password_hash = ?, password_changed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (password_hash, now, now, account_id),
        )
        if revoke_other_sessions:
            _revoke_all_sessions(conn, account_id)
        log_event(
            conn,
            actor=actor,
            action="account.password_change",
            entity_type="account",
            entity_id=account_id,
            summary=f"Password changed for {account.name}",
        )
        return get_account(conn, account_id)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    account: Account


# The single message every failed login returns, whatever went wrong. Telling
# the browser *which* of "no such account", "wrong password" or "not approved
# yet" applied would let anyone enumerate who has an account here.
_GENERIC_FAILURE = "Email or password is incorrect, or the account is not yet approved."


def login(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
) -> LoginResult:
    """Authenticate and open a session.

    Raises :class:`AuthError` with a deliberately uninformative message on
    every failure path. Lockout is checked first and counted in the database,
    so restarting the service does not reset an attacker's budget.
    """
    email = security.normalize_email(email)

    lockout = security.check_lockout(conn, email=email, ip=ip)
    if lockout.locked:
        # Deliberately NOT recorded as a failed attempt. The password was
        # never checked, so there is nothing to count -- and counting it fed
        # the very window that caused the lockout, which made the lockout
        # self-sustaining: five guesses every fifteen minutes kept any staff
        # account locked out permanently, with no way to clear it.
        #
        # The event is written once per address per window, not once per
        # attempt. Because nothing throttled this path and it never reaches
        # scrypt, it was the cheapest way to write to the database in the
        # whole application: an attacker who tripped a lockout could then
        # append thousands of rows a second to the audit log, each one taking
        # the single write lock the counter needs. The log wants to know that
        # this account was attacked and when, which one row says as well as
        # ten thousand.
        if security.lockout_log_throttle.allow(f"lockout:{email or 'unknown'}"):
            with db.transaction(conn):
                log_event(
                    conn,
                    actor=Actor(name=email or "unknown", email=email),
                    action="auth.lockout",
                    entity_type="account",
                    summary=f"Login blocked for {email or 'unknown'}: {lockout.reason}",
                )
        raise AuthError(
            "Too many failed attempts. Wait 15 minutes and try again."
        )

    account = find_by_email(conn, email)

    if account is None:
        # Spend the same CPU as a real verification so that response time does
        # not reveal whether the address exists.
        security.dummy_verify()
        _record_failure(conn, email=email, ip=ip, note="no such account")
        raise AuthError(_GENERIC_FAILURE)

    if not account.can_use_password:
        # An SSO account has an empty hash. verify_password would refuse it
        # anyway -- an empty string does not parse -- but it would refuse it
        # *quickly*, and that difference is measurable. Spend the same CPU as
        # a real verification and return the same message, so this does not
        # become a way to ask which addresses sign in with RIT.
        security.dummy_verify()
        _record_failure(conn, email=email, ip=ip, note="sso-only account")
        raise AuthError(_GENERIC_FAILURE)

    result = security.verify_password(password, account.password_hash)
    if not result.ok:
        _record_failure(conn, email=email, ip=ip, note="bad password")
        raise AuthError(_GENERIC_FAILURE)

    if not account.is_active:
        _record_failure(conn, email=email, ip=ip, note=f"account {account.status}")
        raise AuthError(_GENERIC_FAILURE)

    # Correct password on weaker parameters: upgrade it now, while the
    # plaintext is in hand. This is the only moment that is possible.
    if result.needs_rehash:
        upgraded = security.hash_password(password)
        with db.transaction(conn):
            conn.execute(
                "UPDATE account SET password_hash = ? WHERE id = ?",
                (upgraded, account.id),
            )

    token = security.new_token()
    with db.transaction(conn):
        security.record_attempt(conn, email=email, ip=ip, success=True)
        _create_session(conn, account_id=account.id, token=token,
                        ip=ip, user_agent=user_agent)
        conn.execute(
            "UPDATE account SET last_login_at = ? WHERE id = ?",
            (db.utcnow(), account.id),
        )
        log_event(
            conn,
            actor=account.as_actor(),
            action="auth.login",
            entity_type="account",
            entity_id=account.id,
            summary=f"{account.name} signed in",
        )
    return LoginResult(token=token, account=get_account(conn, account.id))


def link_sso(
    conn: sqlite3.Connection, *, actor: Actor, account_id: int, sso_uid: str
) -> Account:
    """Attach an RIT identity to an existing account, on purpose.

    `sso_login` links a plain `requester` by email on its own, because there
    is nothing there to take: the worst case is somebody borrowing under
    somebody else's name, and RIT vouched for the address. It will not do that
    for a `staff` or `admin` account, and this is what does it instead.

    The difference is what an email match is worth. `uid` is stable; an
    address is a label, and RIT reissue addresses after people leave. Matching
    on one is fine for provisioning and wrong for inheriting a role that can
    write off equipment -- so promoting an identity into a privileged account
    is a decision a human makes, from a shell on the Pi, exactly as making the
    first administrator is.

    Callable before anybody signs in, which is the intended use: link the
    handful of staff accounts the week before the migration term starts.
    """
    sso_uid = (sso_uid or "").strip()
    if not sso_uid:
        raise ValidationError("An RIT uid is required.")

    with db.transaction(conn):
        account = get_account(conn, account_id)

        if account.sso_uid == sso_uid:
            return account
        if account.sso_uid:
            raise ConflictError(
                f"{account.email} is already linked to RIT uid "
                f"{account.sso_uid!r}. Unlinking is not supported; make a new "
                "account if the person has changed."
            )

        clash = _find_by_sso_uid(conn, sso_uid)
        if clash is not None:
            raise ConflictError(
                f"RIT uid {sso_uid!r} is already linked to {clash.email}."
            )

        conn.execute(
            "UPDATE account SET sso_uid = ?, updated_at = ? WHERE id = ?",
            (sso_uid, db.utcnow(), account_id),
        )
        log_event(
            conn,
            actor=actor,
            action="account.sso_link",
            entity_type="account",
            entity_id=account_id,
            summary=(
                f"Linked {account.email} ({account.role}) to RIT single "
                f"sign-on as {sso_uid}"
            ),
            changes={"sso_uid": {"from": account.sso_uid, "to": sso_uid}},
        )
    return get_account(conn, account_id)


def _find_by_sso_uid(conn: sqlite3.Connection, sso_uid: str) -> Account | None:
    row = conn.execute(
        "SELECT * FROM account WHERE sso_uid = ?", (sso_uid,)
    ).fetchone()
    return Account.from_row(row) if row else None


def sso_login(
    conn: sqlite3.Connection,
    *,
    sso_uid: str,
    email: str,
    first_name: str,
    last_name: str,
    affiliation: str = "",
    ip: str = "",
    user_agent: str = "",
    actor: Actor | None = None,
) -> LoginResult:
    """Sign in someone RIT has just vouched for, provisioning them if new.

    The caller has already validated the assertion and spent the handshake --
    see `web/routes_sso.assertion_consumer`. By the time this runs, the
    identity is proved; what is left is deciding which stockroom account it
    belongs to.

    Matching is by `sso_uid` first and email only as a fallback, because that
    is the order of trustworthiness: RIT's `uid` is stable, and an address is
    a label that can be reassigned. An existing password account is found by
    email exactly once -- on first SSO sign-in -- and stamped with its uid,
    which is what lets everyone's role and history survive the migration
    without anybody re-registering.

    Unlike `login`, this does not raise the deliberately vague
    :data:`_GENERIC_FAILURE`. There is nothing to enumerate: the caller
    already knows who they are, because RIT just told them.
    """
    sso_uid = (sso_uid or "").strip()
    if not sso_uid:
        raise ValidationError("The identity provider sent no user id.")
    email = security.normalize_email(_require(email, "Email"))
    if not security.is_institutional_email(email):
        # Defensive: RIT's IdP should never assert anything else. If it does,
        # that is a misconfiguration and not something to quietly accept.
        raise ValidationError(f"{email} is not an RIT address.")
    first_name = (first_name or "").strip() or email.split("@")[0]
    last_name = (last_name or "").strip()

    provisioned = False
    with db.transaction(conn):
        account = _find_by_sso_uid(conn, sso_uid)

        if account is None:
            by_email = find_by_email(conn, email)
            if by_email is not None and by_email.sso_uid:
                # Two RIT identities claiming one address. Never guess.
                raise ConflictError(
                    f"{email} is already linked to a different RIT account."
                )
            if by_email is not None and by_email.role != "requester":
                # An email match is not enough to inherit `staff` or `admin`.
                # Addresses get reissued after people leave; `uid` does not,
                # which is why it is the primary key here. Silently adopting a
                # privileged account on a name collision is the one way this
                # flow could hand out real authority by accident, so it does
                # not: `link_sso` exists to do it deliberately.
                #
                # Raised inside the transaction, so nothing is written. The
                # person is told what to ask for, because they are a real
                # member of staff standing at the counter and "no" with no
                # instructions is useless.
                raise ConflictError(
                    f"{email} is a {by_email.role} account and will not be "
                    "linked to RIT sign-in automatically. Ask an "
                    "administrator to run `stockroom user link-sso` for it."
                )
            account = by_email

        now = db.utcnow()
        if account is None:
            provisioned = True
            status = "active" if config.SSO_AUTO_APPROVE else "pending"
            cur = conn.execute(
                """
                INSERT INTO account (first_name, last_name, email, password_hash,
                                     role, status, created_at, updated_at,
                                     password_changed_at, sso_uid, auth_source,
                                     affiliation, last_sso_login_at)
                VALUES (?, ?, ?, '', 'requester', ?, ?, ?, ?, ?, 'sso', ?, ?)
                """,
                (first_name, last_name, email, status, now, now, now,
                 sso_uid, affiliation or "", now),
            )
            account_id = int(cur.lastrowid)
            log_event(
                conn,
                actor=actor or Actor(name=f"{first_name} {last_name}", email=email),
                action="account.sso_provision",
                entity_type="account",
                entity_id=account_id,
                summary=(
                    f"Created account {first_name} {last_name} <{email}> "
                    f"from RIT single sign-on ({status})"
                ),
                changes={"role": {"from": None, "to": "requester"},
                         "status": {"from": None, "to": status}},
            )
        else:
            account_id = account.id
            changes: dict[str, dict[str, Any]] = {}
            if not account.sso_uid:
                changes["sso_uid"] = {"from": None, "to": sso_uid}
            # The identity provider is authoritative for a person's name.
            if first_name != account.first_name:
                changes["first_name"] = {"from": account.first_name, "to": first_name}
            if last_name != account.last_name:
                changes["last_name"] = {"from": account.last_name, "to": last_name}
            if (affiliation or "") != account.affiliation:
                changes["affiliation"] = {"from": account.affiliation,
                                          "to": affiliation or ""}
            conn.execute(
                "UPDATE account SET sso_uid = ?, first_name = ?, last_name = ?, "
                "affiliation = ?, last_sso_login_at = ?, updated_at = ? WHERE id = ?",
                (sso_uid, first_name, last_name, affiliation or "", now, now,
                 account_id),
            )
            if changes:
                log_event(
                    conn,
                    actor=actor or Actor(name=f"{first_name} {last_name}", email=email),
                    action="account.sso_link",
                    entity_type="account",
                    entity_id=account_id,
                    summary=f"Linked {email} to RIT single sign-on ({sso_uid})",
                    changes=changes,
                )
        account = get_account(conn, account_id)

    # Outside the transaction above on purpose. A provisioned account that is
    # waiting for approval must SURVIVE this refusal -- rolling it back would
    # leave staff with nothing to approve and the person unable to do anything
    # but try again forever.
    if not account.is_active:
        raise AuthError(
            "Your stockroom account is not active yet. "
            "Stockroom staff have been asked to approve it."
            if account.status == "pending"
            else "This stockroom account has been disabled."
        )

    if provisioned or account.person_id is None:
        _link_person(conn, actor=actor or account.as_actor(), account=account)
        account = get_account(conn, account.id)

    token = security.new_token()
    with db.transaction(conn):
        _create_session(conn, account_id=account.id, token=token,
                        ip=ip, user_agent=user_agent)
        conn.execute(
            "UPDATE account SET last_login_at = ? WHERE id = ?",
            (db.utcnow(), account.id),
        )
        log_event(
            conn,
            actor=account.as_actor(),
            action="auth.sso_login",
            entity_type="account",
            entity_id=account.id,
            summary=f"{account.name} signed in with RIT single sign-on",
        )
    return LoginResult(token=token, account=get_account(conn, account.id))


def _record_failure(
    conn: sqlite3.Connection, *, email: str, ip: str, note: str
) -> None:
    with db.transaction(conn):
        security.record_attempt(conn, email=email, ip=ip, success=False)
        log_event(
            conn,
            actor=Actor(name=email or "unknown", email=email),
            action="auth.login_failed",
            entity_type="account",
            summary=f"Failed sign-in for {email or 'unknown'} ({note})",
        )


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def _create_session(
    conn: sqlite3.Connection, *, account_id: int, token: str, ip: str, user_agent: str
) -> int:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cur = conn.execute(
        """
        INSERT INTO session (account_id, token_hash, csrf_token, created_at,
                             last_seen_at, expires_at, absolute_expires_at,
                             ip, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            security.token_hash(token),
            security.new_token(),
            now.strftime(fmt),
            now.strftime(fmt),
            (now + timedelta(hours=IDLE_TIMEOUT_HOURS)).strftime(fmt),
            (now + timedelta(days=ABSOLUTE_TIMEOUT_DAYS)).strftime(fmt),
            ip[:64],
            user_agent[:256],
        ),
    )
    return int(cur.lastrowid)


def resolve_session(
    conn: sqlite3.Connection, token: str
) -> tuple[Session, Account] | None:
    """Look up a live session by its cookie token, or None.

    Also slides the idle expiry forward, at most once every
    :data:`HEARTBEAT_SECONDS`. That write is the audit rule's one documented
    exception: a heartbeat is not a domain change, and recording every page
    view would bury the inventory history.
    """
    if not token:
        return None

    row = conn.execute(
        "SELECT * FROM session WHERE token_hash = ?", (security.token_hash(token),)
    ).fetchone()
    if row is None:
        return None

    session = Session.from_row(row)
    now = db.utcnow()
    if session.revoked_at is not None:
        return None
    if now >= session.expires_at or now >= session.absolute_expires_at:
        return None

    account = get_account(conn, session.account_id)
    if not account.is_active:
        # Disabled between requests: stop honouring the session immediately.
        return None

    if _seconds_since(session.last_seen_at, now) >= HEARTBEAT_SECONDS:
        fresh = (
            datetime.now(timezone.utc) + timedelta(hours=IDLE_TIMEOUT_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Never extend past the absolute cap.
        new_expiry = min(fresh, session.absolute_expires_at)
        with db.transaction(conn):
            conn.execute(
                "UPDATE session SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                (now, new_expiry, session.id),
            )
    return session, account


def _seconds_since(stamp: str, now: str) -> float:
    """How long ago ``stamp`` was, given ``now``, both db.utcnow() strings.

    A malformed or future stamp reads as "long ago", so the heartbeat writes
    rather than skips -- the failure that costs a write is much better than
    the one that lets a session drift towards an expiry that never moves.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        then = datetime.strptime(stamp, fmt)
        current = datetime.strptime(now, fmt)
    except (TypeError, ValueError):
        return float("inf")
    return (current - then).total_seconds()


def logout(conn: sqlite3.Connection, *, token: str, actor: Actor | None = None) -> None:
    """End one session."""
    resolved = conn.execute(
        "SELECT * FROM session WHERE token_hash = ?", (security.token_hash(token),)
    ).fetchone()
    if resolved is None:
        return
    session = Session.from_row(resolved)
    with db.transaction(conn):
        conn.execute(
            "UPDATE session SET revoked_at = ? WHERE id = ?", (db.utcnow(), session.id)
        )
        account = get_account(conn, session.account_id)
        log_event(
            conn,
            actor=actor or account.as_actor(),
            action="auth.logout",
            entity_type="account",
            entity_id=account.id,
            summary=f"{account.name} signed out",
        )


def _revoke_all_sessions(conn: sqlite3.Connection, account_id: int) -> int:
    cur = conn.execute(
        "UPDATE session SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
        (db.utcnow(), account_id),
    )
    return cur.rowcount


def revoke_all_sessions(
    conn: sqlite3.Connection, *, actor: Actor, account_id: int
) -> int:
    """Sign an account out everywhere."""
    with db.transaction(conn):
        account = get_account(conn, account_id)
        count = _revoke_all_sessions(conn, account_id)
        if count:
            log_event(
                conn,
                actor=actor,
                action="auth.sessions_revoked",
                entity_type="account",
                entity_id=account_id,
                summary=f"Revoked {count} session(s) for {account.name}",
            )
        return count


def list_sessions(conn: sqlite3.Connection, account_id: int) -> list[Session]:
    rows = conn.execute(
        "SELECT * FROM session WHERE account_id = ? AND revoked_at IS NULL "
        "AND expires_at > ? ORDER BY last_seen_at DESC",
        (account_id, db.utcnow()),
    )
    return [Session.from_row(r) for r in rows]


def prune_sessions(conn: sqlite3.Connection) -> int:
    """Delete sessions that are dead. Called from the nightly job."""
    with db.transaction(conn):
        cur = conn.execute(
            "DELETE FROM session WHERE absolute_expires_at < ? OR "
            "(revoked_at IS NOT NULL AND revoked_at < ?)",
            (db.utcnow(), db.utcnow()),
        )
        return cur.rowcount
