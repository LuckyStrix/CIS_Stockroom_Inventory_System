"""Security primitives: password hashing, tokens, CSRF, rate limiting.

Everything here is standard library. Password hashing is `hashlib.scrypt`,
tokens come from `secrets`, comparisons use `hmac.compare_digest`. That is a
deliberate choice, not a compromise: this runs unattended on a Raspberry Pi
that somebody has to keep patched, and every compiled dependency is another
thing that can fail to build on an ARM box at 2am after an OS upgrade.

Threat model, stated plainly (and in docs/security.md):

* The service is reachable from the RIT LAN and RIT VPN only. Nothing from the
  internet can reach it.
* Passwords are the weak point and the reason SSO is still the goal. Until
  then they are hashed with a memory-hard function, never logged, and never
  compared without constant time.
* The database is assumed to be readable by an attacker who gets the SD card.
  Hence: password hashes not passwords, and session *token hashes* not tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass

from . import db

# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------

# scrypt work factors. OWASP's password-storage guidance lists these as
# equivalent minimums: (2^17,8,1), (2^16,8,2), (2^15,8,3), (2^14,8,5).
#
# n=2^15, r=8, p=3 is chosen because total work is proportional to n*p while
# peak *memory* is 128*n*r -- so this pairing does the same work as the others
# for 32 MiB instead of 64 or 128 MiB. On a Raspberry Pi that difference is the
# one that matters.
#
# The parameters are stored inside every hash, so raising them later does not
# invalidate existing passwords: verify_password() reports needs_rehash and the
# login path upgrades the stored hash while it still holds the plaintext.
# `stockroom benchmark-hash` measures the actual machine and recommends a set.
#
# Note this is also why login is rate limited below: a deliberately expensive
# KDF is a CPU amplifier, and unbounded login attempts on a Pi would be a
# denial-of-service vector rather than just a guessing one.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 3
SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 12

# A small embedded list of passwords that are common enough to be tried first
# in any real attack. Not a substitute for a full breach corpus -- it is the
# high-value head of the distribution, which is what a length rule alone misses.
_COMMON_PASSWORDS = frozenset("""
password password1 password123 passw0rd p@ssword p@ssw0rd 123456 1234567
12345678 123456789 1234567890 qwerty qwerty123 qwertyuiop abc123 letmein
welcome welcome1 welcome123 monkey dragon baseball football iloveyou
trustno1 sunshine princess admin admin123 administrator root toor
login changeme secret starwars whatever zaq12wsx 1q2w3e4r 1qaz2wsx
asdfghjkl zxcvbnm superman batman pokemon computer internet samsung
google facebook michael jennifer jordan hunter thomas charlie
rochester rit ritrit rittigers ritigers tigers tigerpride imaging imagingscience
carlson carlsoncenter stockroom stockroom1 chester brickcity
""".split())


class PasswordError(ValueError):
    """A password was rejected. The message is safe to show the user."""


# Leetspeak substitutions attackers try first, folded back before comparison.
_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t",
                       "8": "b", "@": "a", "$": "s", "!": "i"})


def _variants(password: str) -> set[str]:
    """Normalisations of a password to test against the common-password list.

    Real cracking rule sets do not try a wordlist verbatim; they append digits,
    substitute leetspeak, and capitalise. So "password123456" and "P@ssw0rd!24"
    are both just "password" with a rule applied, and a literal-match check
    would wave both through while a length rule called them strong.

    Order matters: trailing padding is stripped *before* leetspeak is folded,
    because folding first turns the "1" in "password123" into an "l" and
    destroys the very thing being stripped.
    """
    folded = password.lower()
    trimmed = folded.rstrip("0123456789!@#$%^&*()_+-=.,?~ ")
    return {
        folded,
        trimmed,
        "".join(c for c in folded.translate(_LEET) if c.isalpha()),
        "".join(c for c in trimmed.translate(_LEET) if c.isalpha()),
    }


def check_password_strength(
    password: str, *, email: str = "", first_name: str = "", last_name: str = ""
) -> None:
    """Raise :class:`PasswordError` if a password is unacceptable.

    Follows current NIST guidance: length is the requirement that matters, and
    composition rules ("must contain a symbol") are not imposed because they
    push people towards predictable substitutions. What is checked instead is
    that the password is long, is not one of the obvious guesses, and is not
    simply the user's own name or username.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > 1024:
        # Not a policy -- a guard against someone hashing a 10 MB request body.
        raise PasswordError("Password is too long.")

    folded = password.lower()
    # Check the alphabetic core as well as the literal string: "password123456"
    # and "P@ssw0rd!" are the same guess as "password" to anyone attacking this,
    # and an exact-match list alone would wave both through.
    if _variants(password) & _COMMON_PASSWORDS:
        raise PasswordError("That password is too common. Choose something else.")

    # A password built from the account's own details is guessable by anyone
    # who can read the staff directory.
    local_part = email.split("@")[0].lower() if email else ""
    for personal in (local_part, first_name.lower(), last_name.lower()):
        if personal and len(personal) >= 3 and personal in folded:
            raise PasswordError(
                "Password must not contain your name or username."
            )

    if len(set(password)) < 5:
        raise PasswordError("Password is too repetitive. Choose something else.")


def hash_password(
    password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
) -> str:
    """Hash a password. Returns ``scrypt$n$r$p$salt$hash``, all base64."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        maxmem=_maxmem(n, r, p), dklen=32,
    )
    return "$".join([
        "scrypt", str(n), str(r), str(p),
        base64.b64encode(salt).decode(), base64.b64encode(derived).decode(),
    ])


def _maxmem(n: int, r: int, p: int) -> int:
    """Memory ceiling for scrypt, with headroom over the theoretical need."""
    return int(128 * n * r * 1.5) + (128 * r * p) + (1 << 20)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    needs_rehash: bool = False


def verify_password(password: str, stored: str) -> VerifyResult:
    """Check a password against a stored hash, in constant time.

    ``needs_rehash`` is set when the stored hash used weaker parameters than
    the current defaults, so the caller can quietly upgrade it while it holds
    the plaintext -- the only moment it can.
    """
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_hash = stored.split("$")
        if scheme != "scrypt":
            return VerifyResult(False)
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = base64.b64decode(raw_salt)
        expected = base64.b64decode(raw_hash)
    except (ValueError, TypeError):
        # A malformed hash is a failed login, not a crash.
        return VerifyResult(False)

    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            maxmem=_maxmem(n, r, p), dklen=len(expected),
        )
    except ValueError:
        return VerifyResult(False)

    if not hmac.compare_digest(candidate, expected):
        return VerifyResult(False)
    return VerifyResult(True, needs_rehash=(n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P))


def dummy_verify() -> None:
    """Burn the same work as a real verification, for unknown accounts.

    Without this, a login for an address that does not exist returns
    noticeably faster than one that does, which is a timing oracle for account
    enumeration. Called on the unknown-email path so both cost the same.
    """
    hashlib.scrypt(
        b"timing-equalisation", salt=b"\x00" * SALT_BYTES,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R, SCRYPT_P), dklen=32,
    )


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

def new_token() -> str:
    """A 256-bit URL-safe random token, for sessions and CSRF."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """SHA-256 of a session token.

    Session tokens are high-entropy random values, so a plain hash is right
    here -- there is nothing to brute-force, and this is what keeps a database
    copy from being a bag of live sessions. Deliberately *not* scrypt: this
    runs on every authenticated request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison, for CSRF and session tokens.

    An empty token is never valid, so two empty strings compare *unequal*.
    `hmac.compare_digest("", "")` is True, and a caller who forgot to check
    for the empty case separately would have a bypass: a request with no
    token would match a session with no token. Refusing empties here means
    that mistake cannot be made.
    """
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------

# Institutional addresses only: RIT and its subdomains (cs.rit.edu, mail.rit.edu).
_RIT_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*rit\.edu$", re.I)


def is_institutional_email(email: str) -> bool:
    return bool(_RIT_EMAIL.match((email or "").strip()))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# rate limiting / lockout
# ---------------------------------------------------------------------------

MAX_FAILURES_PER_EMAIL = 5
LOCKOUT_WINDOW_MINUTES = 15
MAX_FAILURES_PER_IP = 20


@dataclass(frozen=True, slots=True)
class LockoutState:
    locked: bool
    failures: int = 0
    reason: str = ""


def _window_start(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def record_attempt(
    conn: sqlite3.Connection, *, email: str, ip: str, success: bool
) -> None:
    conn.execute(
        "INSERT INTO auth_attempt (email, ip, at, success) VALUES (?, ?, ?, ?)",
        (normalize_email(email), ip or "", db.utcnow(), 1 if success else 0),
    )


def check_lockout(conn: sqlite3.Connection, *, email: str, ip: str) -> LockoutState:
    """Whether this email or IP has failed too often recently.

    Counted from the last *successful* login for that address, so a correct
    password clears the counter without needing a separate reset. Stored in
    the database rather than in memory so that restarting the service does not
    hand an attacker a fresh allowance.
    """
    since = _window_start(LOCKOUT_WINDOW_MINUTES)
    email = normalize_email(email)

    last_success = conn.execute(
        "SELECT MAX(at) AS at FROM auth_attempt WHERE email = ? AND success = 1",
        (email,),
    ).fetchone()["at"]
    floor = max(since, last_success) if last_success else since

    failures = conn.execute(
        "SELECT COUNT(*) AS n FROM auth_attempt "
        "WHERE email = ? AND success = 0 AND at > ?",
        (email, floor),
    ).fetchone()["n"]
    if failures >= MAX_FAILURES_PER_EMAIL:
        return LockoutState(True, failures, "too many failed attempts for this account")

    if ip:
        ip_failures = conn.execute(
            "SELECT COUNT(*) AS n FROM auth_attempt "
            "WHERE ip = ? AND success = 0 AND at > ?",
            (ip, since),
        ).fetchone()["n"]
        if ip_failures >= MAX_FAILURES_PER_IP:
            return LockoutState(True, ip_failures, "too many failed attempts from this address")

    return LockoutState(False, failures)


def prune_auth_attempts(conn: sqlite3.Connection, *, keep_days: int = 90) -> int:
    """Drop old attempt rows. Called from the nightly backup job."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = conn.execute("DELETE FROM auth_attempt WHERE at < ?", (cutoff,))
    return cur.rowcount


# ---------------------------------------------------------------------------
# the SAML handshake
#
# One row per sign-in that has been started and not yet finished. This lives
# here rather than in accounts.py for the same reason auth_attempt does: it is
# pre-authentication mechanism, not a domain change, so it is outside the
# audit rule by the same argument -- there is no actor yet, and a log of every
# abandoned click on "Sign in" would bury the inventory history. The sign-in
# that results IS audited, by accounts.sso_login.
#
# See the comment above the table in schema.sql for which of the three checks
# does which job. The short version: request_id proves the response answers
# something this server asked, state_token proves it came back to the browser
# that asked, and only the second of those stops login CSRF.
# ---------------------------------------------------------------------------


class HandshakeError(Exception):
    """A SAML response did not match a sign-in this browser started."""


@dataclass(frozen=True, slots=True)
class Handshake:
    request_id: str
    relay_state: str
    return_to: str


def open_saml_handshake(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    state_token: str,
    relay_state: str,
    return_to: str = "/",
    ip: str = "",
    ttl_seconds: int | None = None,
) -> None:
    """Record a sign-in we are about to send to the identity provider.

    `state_token` is the value that goes in the browser's cookie; only its
    hash is stored, for the same reason session tokens are only stored as
    hashes -- a database leak must not hand over live handshakes.
    """
    from datetime import datetime, timedelta, timezone

    from . import config

    ttl = config.SSO_HANDSHAKE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO saml_auth_request "
            "(request_id, state_hash, relay_state, return_to, created_at, "
            " expires_at, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                token_hash(state_token),
                relay_state,
                return_to or "/",
                db.utcnow(),
                expires,
                ip or "",
            ),
        )


def consume_saml_handshake(
    conn: sqlite3.Connection,
    *,
    state_token: str,
    relay_state: str,
) -> Handshake:
    """Spend the sign-in this browser started, or raise :class:`HandshakeError`.

    Looked up by the browser's cookie rather than by the assertion's
    InResponseTo, for two reasons. The practical one is ordering: the request
    ID lives inside the assertion, and the assertion cannot be validated
    until we know which request it claims to answer, so something has to come
    first. The better one is that starting here checks the browser binding
    *before* any attacker-supplied XML is parsed at all.

    Spending happens before the assertion is checked, which means a signature
    failure burns the handshake and the person has to click Sign in again.
    That is the right trade: it costs a legitimate user one click on a path
    that is already failing, and it denies an attacker repeated attempts
    against a live handshake.

    Every failure raises the same exception. The caller renders a 403; the
    reason is logged, not shown, because the detail is useful to somebody
    probing this endpoint and to nobody else.

    The UPDATE is guarded by `consumed_at IS NULL` inside a transaction, so a
    replay loses a race rather than being merely unlikely to win one --
    db.transaction()'s BEGIN IMMEDIATE is what makes that true, the same
    property that stops two people checking out the same last unit.
    """
    if not state_token:
        raise HandshakeError("no sign-in was started in this browser")

    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM saml_auth_request WHERE state_hash = ?",
            (token_hash(state_token),),
        ).fetchone()
        if row is None:
            raise HandshakeError("this sign-in was started in a different browser")
        if not tokens_equal(row["relay_state"], relay_state or ""):
            raise HandshakeError("relay state does not match")
        if row["consumed_at"] is not None:
            raise HandshakeError("this sign-in has already been completed")
        if row["expires_at"] <= db.utcnow():
            raise HandshakeError("this sign-in took too long")

        spent = conn.execute(
            "UPDATE saml_auth_request SET consumed_at = ? "
            "WHERE request_id = ? AND consumed_at IS NULL",
            (db.utcnow(), row["request_id"]),
        )
        if spent.rowcount != 1:
            # Another request won the race between the SELECT and here.
            raise HandshakeError("this sign-in has already been completed")

        return Handshake(
            request_id=row["request_id"],
            relay_state=row["relay_state"],
            return_to=row["return_to"] or "/",
        )


def prune_saml_handshakes(conn: sqlite3.Connection, *, keep_hours: int = 24) -> int:
    """Drop handshakes nobody came back for. Called from `stockroom prune`."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with db.transaction(conn):
        cur = conn.execute(
            "DELETE FROM saml_auth_request WHERE created_at < ?", (cutoff,)
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# generic in-memory throttle, for non-credential endpoints
# ---------------------------------------------------------------------------

class RateLimiter:
    """A small fixed-window counter, keyed by whatever the caller chooses.

    Used for things like "how many requests can one person file per hour" --
    abuse control, not a security boundary, so losing the counts on restart is
    acceptable and keeping them out of the database is worth it.
    """

    def __init__(self, limit: int, per_seconds: float) -> None:
        self.limit = limit
        self.per_seconds = per_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        import time

        now = time.monotonic()
        cutoff = now - self.per_seconds
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


# How often a *blocked* login may write an audit event, per address.
#
# The lockout path is cheaper than a real login -- it never reaches scrypt --
# and it deliberately records nothing in auth_attempt, so nothing throttled it
# either. That made it a better denial of service than the guessing it defends
# against: every refused attempt appended a hash-chained `event` row and took
# the database's single BEGIN IMMEDIATE write lock. Measured at ~3,400 rows a
# second, which fills an SD card and contends with the checkout counter.
#
# One event per address per lockout window says everything the log needs to:
# that this account was under attack, and when. Kept in memory rather than the
# database because losing it on restart costs one extra row -- a far better
# failure than adding a query to a path whose whole purpose is to be cheap.
lockout_log_throttle = RateLimiter(limit=1, per_seconds=LOCKOUT_WINDOW_MINUTES * 60)
