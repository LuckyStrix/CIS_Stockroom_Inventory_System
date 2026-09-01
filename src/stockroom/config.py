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
    STOCKROOM_BARCODE_PREFIX          default "CIS"
    STOCKROOM_PUBLISH_DEBOUNCE        seconds to coalesce republishes (default 2.0)
    STOCKROOM_ALLOWED_HOSTS           comma-separated Host values to accept
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

# Host header values this service will answer to. TrustedHostMiddleware
# rejects anything else, so a forged Host cannot be reflected back into a link.
# The default covers a Pi named `cis-stockroom` reached by name, by mDNS or
# over loopback; set the variable for anything else.
ALLOWED_HOSTS: list[str] = [
    h.strip()
    for h in os.environ.get(
        "STOCKROOM_ALLOWED_HOSTS",
        "cis-stockroom,cis-stockroom.local,localhost,127.0.0.1,testserver",
    ).split(",")
    if h.strip()
]

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
