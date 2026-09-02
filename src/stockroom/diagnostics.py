"""Is this thing still working?

Written for a Raspberry Pi in a stockroom that nobody logs into. The realistic
way this system dies is not a bug -- it is a backup that stopped running eight
months ago, an SD card quietly going bad, or a search index that fell out of
step and made half the inventory unfindable. None of those announce themselves.

Every check here is a **read**. Nothing in this module writes, so it is always
safe to run: from the nightly timer, from the command line, or from the
staff-visible page at /diagnostics. That page exists because a check nobody
runs is not a check, and nobody is going to SSH into the Pi once a month.

A check reports one of three things:

    ok      verified fine
    warn    worth someone's attention, not yet broken
    fail    something is wrong now

Only ``fail`` makes ``stockroom doctor`` exit non-zero, so a warning does not
train people to ignore a red timer.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import accounts, backup_targets, config, db, service

OK, WARN, FAIL = "ok", "warn", "fail"

_MARKS = {OK: "✓", WARN: "!", FAIL: "✗"}

# A nightly timer that has not fired in this long is not "a bit late".
_BACKUP_STALE_HOURS = 48
# Below this, WAL growth and the next snapshot become the problem.
_DISK_WARN_MB = 500
_DISK_FAIL_MB = 100


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != FAIL

    @property
    def mark(self) -> str:
        return _MARKS[self.status]


@dataclass(frozen=True, slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _age_hours(when: datetime) -> float:
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


def _parse(stamp: str | None) -> datetime | None:
    """Parse one of the ISO-8601 UTC strings this codebase stores everywhere."""
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _describe_age(hours: float) -> str:
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f} days ago"


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def check_integrity(conn: sqlite3.Connection) -> Check:
    """SQLite's own opinion of the live database file."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    result = row[0] if row else "no result"
    if result != "ok":
        return Check("database integrity", FAIL, str(result))
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        return Check(
            "database integrity", FAIL,
            f"{len(violations)} foreign key violation(s)",
        )
    return Check("database integrity", OK, "integrity and foreign keys sound")


def check_schema_version(conn: sqlite3.Connection) -> Check:
    stored = db.get_meta(conn, "schema_version")
    if stored is None:
        return Check("schema version", FAIL, "no version recorded")
    if int(stored) != db.SCHEMA_VERSION:
        return Check(
            "schema version", FAIL,
            f"database is at v{stored}, this build expects "
            f"v{db.SCHEMA_VERSION} -- run `stockroom init`",
        )
    return Check("schema version", OK, f"v{stored}")


def check_audit_chain(conn: sqlite3.Connection) -> Check:
    """The audit log is the point of the system; prove it has not been edited."""
    result = service.verify_audit_chain(conn)
    if not result.ok:
        return Check("audit chain", FAIL, str(result))
    return Check("audit chain", OK, str(result))


def check_search_index(conn: sqlite3.Connection) -> Check:
    if not db.fts_enabled(conn):
        return Check(
            "search index", WARN,
            "FTS5 unavailable in this SQLite build; search is falling back "
            "to LIKE, which is correct but slower",
        )
    items = conn.execute("SELECT COUNT(*) AS n FROM item").fetchone()["n"]
    indexed = conn.execute("SELECT COUNT(*) AS n FROM item_fts").fetchone()["n"]
    if items != indexed:
        return Check(
            "search index", FAIL,
            f"{items} items but {indexed} indexed -- items are missing from "
            "search; `stockroom init` rebuilds it",
        )
    return Check("search index", OK, f"{indexed} item(s) indexed")


def check_backups(conn: sqlite3.Connection | None = None) -> Check:
    snapshots = sorted(config.BACKUP_DIR.glob("stockroom-*.db")) \
        if config.BACKUP_DIR.is_dir() else []
    if not snapshots:
        return Check("local backups", FAIL, f"none in {config.BACKUP_DIR}")
    newest = max(snapshots, key=lambda p: p.stat().st_mtime)
    age = _age_hours(
        datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    )
    detail = f"{len(snapshots)} snapshot(s), newest {_describe_age(age)}"
    if age > _BACKUP_STALE_HOURS:
        return Check(
            "local backups", FAIL,
            detail + " -- the nightly timer is not running "
            "(systemctl status stockroom-backup.timer)",
        )
    return Check("local backups", OK, detail)


def check_offsite_backups(*, skip_remote: bool = False) -> Check:
    """Is a copy actually leaving the machine?

    Residual risk 4 in docs/security.md: backups on the same SD card as the
    database are not backups. This reports honestly when nothing is
    configured rather than staying quiet about it.
    """
    targets = backup_targets.configured_targets()
    if not targets:
        return Check(
            "off-box backups", WARN,
            "not configured -- every copy is on the Pi's SD card. Set "
            "STOCKROOM_BACKUP_COPY_DIR or STOCKROOM_BACKUP_REMOTE",
        )

    parts, worst = [], OK
    for target in targets:
        if skip_remote and target.name == "rclone":
            parts.append(f"{target.name}: skipped")
            continue
        try:
            found = target.existing()
        except Exception as exc:
            parts.append(f"{target.name}: unreachable ({exc})")
            worst = FAIL
            continue

        # "empty" is the symptom of several different problems, and by far the
        # most confusing one is a directory the nightly job cannot write to:
        # the backup unit runs under ProtectSystem=strict, so a copy directory
        # outside /var/lib/stockroom is read-only to it while the identical
        # command in an SSH session works. Naming that here is the difference
        # between a five-minute fix and an afternoon.
        unwritable = _unwritable_reason(target)
        if unwritable:
            parts.append(f"{target.name}: {unwritable}")
            worst = FAIL
        elif not found:
            parts.append(f"{target.name}: empty")
            worst = FAIL
        else:
            parts.append(f"{target.name}: {len(found)}, newest {found[-1]}")
    return Check("off-box backups", worst, "; ".join(parts))


def _unwritable_reason(target) -> str:
    """Why this target cannot be written to, or '' if it can.

    Only meaningful for a local directory; a remote's reachability is already
    covered by existing() raising.
    """
    directory = getattr(target, "directory", None)
    if directory is None:
        return ""
    if not directory.is_dir():
        return f"{directory} is not a directory -- is the drive mounted?"
    probe = directory / ".stockroom-write-test"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return (
            f"{directory} is not writable ({exc.strerror}). If this is the "
            "nightly job, the unit needs ReadWritePaths for that path -- "
            "re-run deploy/setup-pi.sh"
        )
    return ""


def _blocked_from_the_web_server() -> str:
    """Whether nginx could actually read the generated page, or only we can.

    It serves /public/ from disk as www-data, so it needs the traverse bit on
    every directory down to the page and r-x on the one holding it. This is a
    different question from "has the page been generated", and from the
    counter the two look identical: try_files reports a permission denial as a
    miss, so the browser gets a 404 for a file that is sitting right there --
    and this check reported the page present all along, because the service
    user owns it and could always see it.

    Returns a description of the first problem, or "" if there is none.
    """
    page = config.PUBLISH_DIR / "index.html"
    wanted: list[tuple[Path, int, str]] = [(config.PUBLISH_DIR, 0o005, "enter")]

    # Everything between the data directory and the page must be walkable too.
    # Only as far as the data directory, though, and no further: on a
    # developer's machine the path above it runs through a home directory
    # whose mode is nobody else's business.
    if config.PUBLISH_DIR.is_relative_to(config.DATA_DIR):
        parent = config.PUBLISH_DIR.parent
        while True:
            wanted.append((parent, 0o001, "traverse"))
            if parent == config.DATA_DIR or parent == parent.parent:
                break
            parent = parent.parent

    wanted.append((page, 0o004, "read"))

    for target, needed, verb in wanted:
        try:
            mode = target.stat().st_mode & 0o777
        except OSError:
            continue  # a missing path is the caller's other checks to report
        if mode & needed != needed:
            return f"nginx cannot {verb} {target} ({mode:04o})"
    return ""


def check_publish(conn: sqlite3.Connection | None = None) -> Check:
    index = config.PUBLISH_DIR / "index.html"
    feed = config.PUBLISH_DIR / "inventory.json"
    if not index.is_file():
        return Check(
            "public page", FAIL,
            f"{index} has never been generated -- run `stockroom publish`",
        )
    try:
        json.loads(feed.read_text())
    except FileNotFoundError:
        return Check("public page", FAIL, "inventory.json is missing")
    except json.JSONDecodeError as exc:
        return Check("public page", FAIL, f"inventory.json is not valid JSON: {exc}")
    age = _age_hours(datetime.fromtimestamp(index.stat().st_mtime, tz=timezone.utc))
    blocked = _blocked_from_the_web_server()
    if blocked:
        return Check(
            "public page", WARN,
            f"rebuilt {_describe_age(age)}, but {blocked} -- /public/ will "
            "answer 404 with the page present (deploy/setup-pi.sh restores "
            "the modes)",
        )
    return Check("public page", OK, f"rebuilt {_describe_age(age)}, feed parses")


def check_disk_space(conn: sqlite3.Connection | None = None) -> Check:
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
    except OSError as exc:
        return Check("disk space", FAIL, f"cannot stat {config.DATA_DIR}: {exc}")
    free_mb = usage.free / (1024 * 1024)
    detail = f"{free_mb:,.0f} MB free on {config.DATA_DIR}"
    if free_mb < _DISK_FAIL_MB:
        return Check("disk space", FAIL, detail)
    if free_mb < _DISK_WARN_MB:
        return Check("disk space", WARN, detail)
    return Check("disk space", OK, detail)


def check_administrators(conn: sqlite3.Connection) -> Check:
    """At least one person must still be able to administer this.

    The realistic version of this failing is the student who set it up
    graduating and their account being disabled.
    """
    admins = [
        a for a in accounts.list_accounts(conn, role="admin") if a.is_active
    ]
    if not admins:
        return Check(
            "administrators", FAIL,
            "no active admin account -- create one on the Pi with "
            "`stockroom user create --admin`",
        )
    return Check("administrators", OK, f"{len(admins)} active")


def check_queues(conn: sqlite3.Connection) -> Check:
    """Nobody is emailed about anything, so a queue is only worked if watched."""
    from . import requests_service

    pending_requests = requests_service.count_pending(conn)
    pending_accounts = accounts.count_pending(conn)
    if not pending_requests and not pending_accounts:
        return Check("waiting queues", OK, "nothing waiting")

    oldest = conn.execute(
        "SELECT MIN(created_at) AS oldest FROM ("
        "  SELECT created_at FROM request WHERE status = 'pending'"
        "  UNION ALL"
        "  SELECT created_at FROM account WHERE status = 'pending')"
    ).fetchone()["oldest"]
    stamp = _parse(oldest)
    age = _age_hours(stamp) if stamp else 0.0
    detail = (
        f"{pending_requests} request(s), {pending_accounts} signup(s); "
        f"oldest {_describe_age(age)}"
    )
    status = WARN if age > config.REQUEST_STALE_DAYS * 24 else OK
    return Check("waiting queues", status, detail)


def check_data_consistency(conn: sqlite3.Connection) -> Check:
    """Cheap sanity queries over states that should be impossible."""
    problems: list[str] = []

    negative = conn.execute(
        "SELECT COUNT(*) AS n FROM item_status WHERE available < 0"
    ).fetchone()["n"]
    if negative:
        problems.append(f"{negative} item(s) with negative availability")

    archived_out = conn.execute(
        "SELECT COUNT(*) AS n FROM loan l JOIN item i ON i.id = l.item_id "
        "WHERE l.returned_at IS NULL AND i.archived_at IS NOT NULL"
    ).fetchone()["n"]
    if archived_out:
        problems.append(f"{archived_out} open loan(s) on archived items")

    unhashed = conn.execute(
        "SELECT COUNT(*) AS n FROM event WHERE hash IS NULL"
    ).fetchone()["n"]
    if unhashed:
        problems.append(f"{unhashed} event(s) with no hash")

    if problems:
        return Check("data consistency", FAIL, "; ".join(problems))
    return Check("data consistency", OK, "no impossible states found")


# ---------------------------------------------------------------------------
# running them
# ---------------------------------------------------------------------------


def run_all(conn: sqlite3.Connection, *, skip_remote: bool = False) -> Report:
    """Run every check. Never raises: a check that explodes is a failed check."""
    checks: list[Check] = []
    for name, run in (
        ("database integrity", lambda: check_integrity(conn)),
        ("schema version", lambda: check_schema_version(conn)),
        ("audit chain", lambda: check_audit_chain(conn)),
        ("search index", lambda: check_search_index(conn)),
        ("data consistency", lambda: check_data_consistency(conn)),
        ("local backups", lambda: check_backups(conn)),
        ("off-box backups", lambda: check_offsite_backups(skip_remote=skip_remote)),
        ("public page", lambda: check_publish(conn)),
        ("disk space", lambda: check_disk_space(conn)),
        ("administrators", lambda: check_administrators(conn)),
        ("waiting queues", lambda: check_queues(conn)),
    ):
        try:
            checks.append(run())
        except Exception as exc:  # a broken check is a finding, not a crash
            checks.append(Check(name, FAIL, f"the check itself failed: {exc!r}"))
    return Report(checks)
