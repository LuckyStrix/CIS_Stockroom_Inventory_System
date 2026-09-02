"""Configuration.

Everything is a plain module-level default that can be overridden with an
environment variable, so the Pi's systemd unit can configure the service
without editing code and the test suite can point at a temp directory.

Anything not already in the environment is filled in from /etc/stockroom.env
(override with STOCKROOM_ENV_FILE), the same file the systemd units read, so a
CLI command run by hand on the Pi sees the installed settings.

Environment variables (all optional):

    STOCKROOM_ENV_FILE      where to read the above from  (default /etc/stockroom.env)
    STOCKROOM_DATA_DIR      where stockroom.db lives      (default <repo>/data)
    STOCKROOM_DB            full path to the database     (overrides the above)
    STOCKROOM_PUBLISH_DIR   where the public site is written (default <repo>/publish)
    STOCKROOM_ORG           heading shown on the public page
    STOCKROOM_PUBLIC_SHOW_BORROWERS   "1" to name borrowers publicly (default off)
    STOCKROOM_GITHUB_PAGES_DIR        enable the GitHub Pages mirror by pointing
                                      this at a local clone of the Pages repo
    STOCKROOM_GITHUB_PAGES_BRANCH     branch to commit to (default "main")
    STOCKROOM_TIMEZONE                the stockroom's wall clock, for reading
                                      typed dates and printing stored ones
                                      (default America/New_York)
    STOCKROOM_BARCODE_PREFIX          default "CIS"
    STOCKROOM_PUBLISH_DEBOUNCE        seconds to coalesce republishes (default 2.0)
    STOCKROOM_ALLOWED_HOSTS           comma-separated Host values to accept
    STOCKROOM_ALLOW_IP_HOSTS          "0" to reject a Host that is a bare IP
    STOCKROOM_SESSION_IDLE_HOURS      idle session timeout (default 8)
    STOCKROOM_SESSION_MAX_DAYS        absolute session cap (default 7)
    STOCKROOM_BACKUP_COPY_DIR         second local copy (a mounted USB stick)
    STOCKROOM_BACKUP_REMOTE           rclone remote, e.g. "gdrive:stockroom"
    STOCKROOM_RCLONE                  path to the rclone binary
    STOCKROOM_BACKUP_REMOTE_KEEP      snapshots to keep on the remote
    STOCKROOM_PHOTO_DIR               where uploaded item photos are stored
    STOCKROOM_PHOTO_MAX_PIXELS        longest edge after downscaling (1600)
    STOCKROOM_MAX_UPLOAD_BYTES        reject a request body larger than this
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

# <repo root>/src/stockroom/config.py -> <repo root>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The installed service gets its settings from /etc/stockroom.env via systemd's
# EnvironmentFile=. Nothing gave them to a CLI invocation, so
#
#     sudo -u stockroom /opt/stockroom/.venv/bin/stockroom user create --admin
#
# -- the very command setup-pi.sh tells the operator to run next -- fell back
# to the defaults below and tried to open <repo root>/data, i.e.
# /opt/stockroom/data, which is root-owned:
#
#     PermissionError: [Errno 13] Permission denied: '/opt/stockroom/data'
#
# Worse than the error: had the directory been writable, it would have silently
# created a SECOND empty database beside the real one in /var/lib/stockroom and
# put the administrator in it.
#
# So read the same file systemd reads, here, once, before any default is
# computed. The environment always wins, which keeps systemd (and the tests'
# monkeypatching) authoritative -- this only fills in what nobody set.
ENV_FILE = Path(os.environ.get("STOCKROOM_ENV_FILE", "/etc/stockroom.env"))


def _load_env_file(path: Path) -> None:
    """Apply `KEY=value` lines from an env file to os.environ.

    Deliberately parsed, never executed: this reads a root-owned file in /etc.
    The grammar is systemd's, not bash's -- everything after the first `=` is
    the literal value, minus one optional layer of matching quotes. Keep it in
    step with load_env_file() in deploy/setup-pi.sh, which has to do the same
    job in shell; tests/test_deploy.py checks the two agree.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return  # absent (the normal case in development) or unreadable
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier() or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


_load_env_file(ENV_FILE)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR: Path = _env_path("STOCKROOM_DATA_DIR", REPO_ROOT / "data")
DB_PATH: Path = _env_path("STOCKROOM_DB", DATA_DIR / "stockroom.db")
BACKUP_DIR: Path = DATA_DIR / "backups"

PUBLISH_DIR: Path = _env_path("STOCKROOM_PUBLISH_DIR", REPO_ROOT / "publish")

# Uploaded item photos. Inside DATA_DIR because that is the only path the
# systemd unit lets the service write to (ReadWritePaths=/var/lib/stockroom).
PHOTO_DIR: Path = _env_path("STOCKROOM_PHOTO_DIR", DATA_DIR / "photos")

# Uploaded photos are re-encoded down to this before being stored. A phone
# photo is 3-5 MB; at this size they land around 200 KB, which matters on an
# SD card that is already the most likely thing here to fail.
PHOTO_MAX_PIXELS: int = int(os.environ.get("STOCKROOM_PHOTO_MAX_PIXELS", "1600"))
PHOTO_QUALITY: int = int(os.environ.get("STOCKROOM_PHOTO_QUALITY", "82"))

# Hard ceiling on an upload body, checked in the CSRF middleware before the
# body is parsed. nginx caps this at 8m in production, but development runs
# without nginx and the middleware reads the whole body into memory.
MAX_UPLOAD_BYTES: int = int(os.environ.get("STOCKROOM_MAX_UPLOAD_BYTES", str(9 * 1024 * 1024)))

ORG_NAME: str = os.environ.get(
    "STOCKROOM_ORG", "Carlson Center for Imaging Science — RIT"
)

# Privacy: the public page shows availability COUNTS only, never who is
# holding something. Turning this on publishes borrower names and emails to
# anyone who can reach the page -- a deliberate decision, not a default.
PUBLIC_SHOW_BORROWERS: bool = _env_bool("STOCKROOM_PUBLIC_SHOW_BORROWERS", False)

# Optional mirror of the generated site to a GitHub Pages repo. Unset = off.
_gh = os.environ.get("STOCKROOM_GITHUB_PAGES_DIR")
GITHUB_PAGES_DIR: Path | None = Path(_gh).expanduser().resolve() if _gh else None
GITHUB_PAGES_BRANCH: str = os.environ.get("STOCKROOM_GITHUB_PAGES_BRANCH", "main")

# Host header values this service will answer to. Anything else is rejected,
# so a forged Host cannot be reflected back into a link.
#
# The default used to be the literal string "cis-stockroom,cis-stockroom.local,
# localhost,127.0.0.1,testserver" -- the hostname docs/raspberry-pi-setup.md
# tells you to give the Pi. A Pi named anything else served every device on the
# LAN a bare 400 "Invalid host header", which says nothing about what is wrong
# or where to fix it.
#
# Deriving it from the machine's own hostname means the name that reaches the
# Pi is the name the Pi accepts, whatever it was called during imaging. That is
# also where the TLS certificate's SANs come from, so the two agree by
# construction.
#
# The fully qualified name has to be looked up separately. `hostname` on Debian
# usually reports the SHORT name, with the qualified one recorded only in
# /etc/hosts -- so taking `gethostname().split(".")[0]` and stopping there threw
# away the name people actually type. A Pi registered as
# cisstockroom.device.rit.edu answered its own short name and refused the FQDN,
# which is the same "Invalid host header" wall this default was written to tear
# down, one layer further in.
def _fqdn() -> str:
    """This machine's qualified name, or "" if it has none.

    getfqdn() reads /etc/hosts before it asks DNS, which is where the name
    lives on a Pi imaged with one. A resolver that is slow or absent must not
    take the service down at import, hence the guard -- an unqualified machine
    simply keeps the short-name-only behaviour.
    """
    try:
        return socket.getfqdn().lower()
    except OSError:  # pragma: no cover - resolver failure
        return ""


def _default_allowed_hosts() -> list[str]:
    reported = socket.gethostname().lower()
    host = reported.split(".")[0]
    names = [host, f"{host}.local"]
    for candidate in (reported, _fqdn()):
        # Must be this machine's own short name plus a domain. That one test
        # rejects everything getfqdn() returns when there is no real domain --
        # "localhost", a bare IP, the short name back again -- without having
        # to enumerate those cases.
        if candidate.startswith(f"{host}.") and candidate not in names:
            names.append(candidate)
    # `testserver` is what Starlette's TestClient sends; the suite would
    # otherwise need every request to carry a Host header.
    return names + ["localhost", "127.0.0.1", "::1", "testserver"]


_LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "::1"]


def _allowed_hosts(configured: str) -> list[str]:
    """The allow list, from the STOCKROOM_ALLOWED_HOSTS value (may be empty).

    Loopback is always in it, even when an operator sets an explicit list. The
    service binds 127.0.0.1 and nothing else, so a loopback Host cannot have
    come from the network -- and it is what the installer's health check,
    `stockroom doctor` and any curl from an SSH session send. Leaving it out of
    a custom list would mean the Pi could not answer itself.
    """
    named = [h.strip().lower() for h in configured.split(",") if h.strip()]
    if not named:
        return _default_allowed_hosts()
    return list(dict.fromkeys(named + _LOOPBACK_HOSTS))


ALLOWED_HOSTS: list[str] = _allowed_hosts(
    os.environ.get("STOCKROOM_ALLOWED_HOSTS", "")
)

# Also answer when the Host is a bare IP address, e.g. https://10.14.2.31/.
#
# This is not a loosening for its own sake. It is the access route
# docs/raspberry-pi-setup.md already documents ("if cis-stockroom.local does
# not resolve, find the Pi's IP on your router"), and on the devices that most
# need it -- Android phones, Chromebooks, anything without an mDNS resolver --
# it is the ONLY route. The check above then rejected it, which is a documented
# instruction that could not work.
#
# What the Host check protects is absolute URLs built from the Host header:
# password-reset links, mails, redirects to a poisoned domain. This application
# builds none -- every use of `request.url` reads `.path`, `.query` or
# `.scheme`, never the host -- and it has no email at all, so there is nothing
# for a forged IP to poison. Names still have to be on the list, and setting
# this to "0" restores the stricter behaviour.
ALLOW_IP_HOSTS: bool = _env_bool("STOCKROOM_ALLOW_IP_HOSTS", True)

# The stockroom's own wall clock.
#
# Everything is STORED in UTC and that does not change -- the timestamps sort
# lexicographically, which the overdue query relies on. This is the timezone
# used at the two boundaries where a human is involved: reading a date typed
# into a form, and printing a stored one back onto a page.
#
# Without it the two were simply skipped, in opposite directions. A time the
# system generated was true UTC and displayed as though it were local, so a
# checkout at 2pm read as 18:00. A date somebody typed was local and got a Z
# stapled to it, so "due Friday" became 23:59:59Z on Friday -- 19:59 in
# Rochester -- and the loan went overdue while the building was still open.
#
# An unknown zone name would otherwise raise at import and take the service
# down at boot, which is a poor trade for a setting almost nobody will change.
def _load_timezone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        import logging

        logging.getLogger("stockroom").warning(
            "Unknown STOCKROOM_TIMEZONE %r; falling back to America/New_York. "
            "Use a tz database name such as America/New_York.", name,
        )
        return ZoneInfo("America/New_York")


TIMEZONE_NAME: str = os.environ.get("STOCKROOM_TIMEZONE", "America/New_York")
TIMEZONE = _load_timezone(TIMEZONE_NAME)

BARCODE_PREFIX: str = os.environ.get("STOCKROOM_BARCODE_PREFIX", "CIS")
BARCODE_DIGITS: int = 6  # CIS-000142

# A burst of changes (a CSV import, rapid check-ins) should render the public
# site once, not once per row.
PUBLISH_DEBOUNCE_SECONDS: float = float(
    os.environ.get("STOCKROOM_PUBLISH_DEBOUNCE", "2.0")
)

# Number of nightly database snapshots to keep (see deploy/stockroom-backup).
BACKUP_KEEP: int = int(os.environ.get("STOCKROOM_BACKUP_KEEP", "30"))

# Where else a snapshot goes once it has been written and verified. Both are
# opt-in: unset means the only copy lives on the Pi's SD card, which is the
# single most likely component to fail. See docs/operations.md.
#
# A second local directory -- a mounted USB stick, or a share.
_copy = os.environ.get("STOCKROOM_BACKUP_COPY_DIR")
BACKUP_COPY_DIR: Path | None = Path(_copy).expanduser().resolve() if _copy else None

# An rclone remote, e.g. "gdrive:stockroom-backups". rclone owns the
# credentials (~stockroom/.config/rclone/rclone.conf, created once with
# `sudo -u stockroom rclone config`); this application never sees a token.
#
# Note what ends up there: a snapshot is a readable copy of the whole
# database -- every email address and the entire audit log. Keep the
# destination folder private to the account that owns it.
BACKUP_REMOTE: str = os.environ.get("STOCKROOM_BACKUP_REMOTE", "").strip()
RCLONE: str = os.environ.get("STOCKROOM_RCLONE", "rclone")
BACKUP_REMOTE_KEEP: int = int(os.environ.get("STOCKROOM_BACKUP_REMOTE_KEEP", "30"))

# How long a pending request or signup may sit before it is flagged as stale.
# There is no email server, so nothing chases anyone: a request is only worked
# if a human sees it waiting. This is what turns "waiting" into "waiting too
# long" in the inbox and in `stockroom doctor`.
REQUEST_STALE_DAYS: int = int(os.environ.get("STOCKROOM_REQUEST_STALE_DAYS", "3"))

# Session lifetimes. Idle expiry slides forward on each request; the absolute
# cap never does.
SESSION_IDLE_HOURS: int = int(os.environ.get("STOCKROOM_SESSION_IDLE_HOURS", "8"))
SESSION_MAX_DAYS: int = int(os.environ.get("STOCKROOM_SESSION_MAX_DAYS", "7"))
