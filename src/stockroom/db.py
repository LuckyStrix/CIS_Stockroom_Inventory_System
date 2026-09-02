"""Database connection, schema management and the transaction helper.

Design notes
------------
* **WAL mode.** Readers never block the writer and vice versa, which matters
  because the publisher reads the whole inventory on a background thread
  while people are checking things in and out.

* **One connection per thread.** ``sqlite3`` connections are not safe to
  share across threads, so :func:`connect` hands out a thread-local
  connection. FastAPI runs sync endpoints in a threadpool, so this is the
  behaviour that keeps things simple.

* **``busy_timeout``.** Two simultaneous checkouts are a real possibility at
  a shared counter. Rather than surfacing "database is locked" to a student,
  the second writer waits up to five seconds for the first to commit.

* **``transaction()`` is the only way to write.** It opens an IMMEDIATE
  transaction, so the write lock is taken before any read inside the block.
  That is what makes the check-availability-then-insert-loan sequence in
  :mod:`stockroom.service` atomic rather than a race.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 5

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")
_SCHEMA_FTS_SQL = Path(__file__).with_name("schema_fts.sql")

_local = threading.local()


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------
def utcnow() -> str:
    """Current time as an ISO-8601 UTC string, the format every column uses.

    Second precision -- these are human-facing audit timestamps, and the
    extra microseconds only make the history harder to read.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------
def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # We manage transactions explicitly via transaction(); tell the driver to
    # stay out of the way rather than opening implicit ones.
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return this thread's connection, opening it on first use."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    key = str(path)
    # `getattr(..., None) or {}` would be wrong here: an *empty* cache is
    # falsy, so it would build a fresh dict that nothing ever stores back, and
    # every later call would open an untracked connection that close_all()
    # cannot close. That is the state every test is left in, because
    # conftest.py calls close_all() between them. Test for None, not falsiness.
    cache: dict[str, sqlite3.Connection] | None = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}

    conn = cache.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=5.0)
        _configure(conn)
        cache[key] = conn
    return conn


def _discard(conn: sqlite3.Connection) -> None:
    """Forget a connection that can no longer be trusted, and close it.

    Only transaction() calls this, and only when a COMMIT or ROLLBACK failed:
    at that point the connection may still be inside a transaction, and since
    connect() caches one per thread forever, every later request on that
    thread would inherit the problem.
    """
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    for key, cached in list(cache.items()):
        if cached is conn:
            del cache[key]
    try:
        conn.close()
    except sqlite3.Error:
        pass


def close_all() -> None:
    """Close this thread's connections. Used by tests and CLI teardown."""
    for conn in getattr(_local, "conns", {}).values():
        conn.close()
    _local.conns = {}


def has_fts5(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build has FTS5 (search.py falls back to LIKE)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def fts_enabled(conn: sqlite3.Connection) -> bool:
    """Whether the item_fts index actually exists in this database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_fts'"
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# transactions
# --------------------------------------------------------------------------
@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single IMMEDIATE transaction.

    IMMEDIATE (rather than the default DEFERRED) takes the write lock up
    front. Without it, two people checking out the last unit of an item could
    both read "available: 1" before either wrote, and one would then fail at
    COMMIT time with a confusing error instead of a clean "not enough
    available" message.

    Nested use is supported and joins the outer transaction, so service-layer
    functions can call one another and still commit exactly once.
    """
    conn = conn if conn is not None else connect()

    if getattr(_local, "depth", 0) > 0:
        _local.depth += 1
        try:
            yield conn
        finally:
            _local.depth -= 1
        return

    conn.execute("BEGIN IMMEDIATE")
    _local.depth = 1
    try:
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    except sqlite3.Error:
        # The COMMIT or the ROLLBACK itself failed -- a full SD card is the
        # realistic way that happens here. The transaction may still be open
        # on this connection, and connections are cached per thread and never
        # otherwise discarded, so the next BEGIN IMMEDIATE on this thread
        # would raise "cannot start a transaction within a transaction" and go
        # on doing so forever. One disk-full moment would wedge a worker
        # thread for the life of the process.
        #
        # Dropping the connection costs one reconnect and leaves the pool
        # healthy. The original error still propagates.
        _discard(conn)
        raise
    finally:
        _local.depth = 0


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# Columns added to tables that already existed in an earlier schema version.
# CREATE TABLE IF NOT EXISTS cannot express these: the table is already there,
# so the new column in schema.sql is ignored for anyone upgrading.
#
# Every entry is also declared in schema.sql, so a fresh database gets it from
# the CREATE TABLE and an existing one gets it from here. The two are checked
# against each other by test_a_migrated_database_matches_a_fresh_one.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # v3: tamper-evident audit log (see service.log_event)
    ("event", "prev_hash", "TEXT"),
    ("event", "hash", "TEXT"),
    # v3: per-unit tracking opt-in (see service.create_unit)
    ("item", "tracked", "INTEGER NOT NULL DEFAULT 0"),
    # v3: duplicate-person merges (see service.merge_people)
    ("person", "merged_into_id", "INTEGER REFERENCES person(id)"),
    # v4: which individual unit went out on this loan (see service.checkout).
    # The forward reference to `unit` is fine even on a v2 database, where
    # that table does not exist yet when this runs: SQLite resolves foreign
    # keys when a row is written, not when the column is declared.
    ("loan", "unit_id", "INTEGER REFERENCES unit(id)"),
    # v5: single sign-on. `sso_uid` is RIT's `uid` attribute -- the stable
    # identifier, unlike email, which people do change. `auth_source` says
    # which door an account came in through, so a password can never be set
    # on an SSO account or vice versa. `affiliation` is ritEduAffiliation,
    # recorded because ITS asks what we store, and deliberately NOT used to
    # decide roles: "Employee" is a much larger set than "works here".
    #
    # sso_uid wants to be UNIQUE and cannot be: ALTER TABLE cannot add a
    # constraint. The uniqueness is a partial index in schema.sql instead,
    # which reaches both a fresh and an upgrading database because indexes
    # run after this.
    ("account", "sso_uid", "TEXT"),
    ("account", "auth_source", "TEXT NOT NULL DEFAULT 'password'"),
    ("account", "affiliation", "TEXT NOT NULL DEFAULT ''"),
    ("account", "last_sso_login_at", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add any column in :data:`_ADDED_COLUMNS` that this database lacks.

    Runs *before* schema.sql, not after, because the views in that file select
    columns added here -- creating the view against an old table would fail
    before the migration ever got a chance to run.

    Idempotent in both directions: a brand new database has none of the tables
    yet and skips every entry, then schema.sql creates them with the columns
    already declared.
    """
    for table, column, decl in _ADDED_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column in _column_names(conn, table):
            continue
        # ALTER TABLE ADD COLUMN cannot take a non-constant default, which is
        # why every entry above is either nullable or has a literal default.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create or upgrade the schema. Safe to run on every start.

    Every statement is ``IF NOT EXISTS`` or ``DROP``-then-``CREATE`` for
    views and triggers, so this is idempotent -- the systemd unit runs it at
    boot so a fresh Pi comes up with a working database and no manual step.
    """
    conn = connect(db_path)

    # Widen existing tables first: schema.sql's views select columns that an
    # older database does not have yet.
    _ensure_columns(conn)

    conn.executescript(_SCHEMA_SQL.read_text())

    if has_fts5(conn):
        conn.executescript(_SCHEMA_FTS_SQL.read_text())
        _backfill_fts(conn)

    with transaction(conn):
        existing = get_meta(conn, "schema_version")
        if existing is None:
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
            set_meta(conn, "created_at", utcnow())
        elif int(existing) != SCHEMA_VERSION:
            _migrate(conn, from_version=int(existing))
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        if get_meta(conn, "barcode_counter") is None:
            set_meta(conn, "barcode_counter", "0")
    return conn


def _migrate(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Carry an older database forward to :data:`SCHEMA_VERSION`.

    The schema file itself is written entirely in ``CREATE ... IF NOT EXISTS``
    and ``DROP``-then-``CREATE`` form, so *adding* tables, views and indexes
    needs no work here -- running the file above already did it. This function
    exists for the steps that cannot be expressed that way: backfilling a new
    column, or reshaping existing rows.

    New *columns* are handled by :func:`_ensure_columns`, which runs earlier
    and unconditionally. What is left for this function is data: backfilling a
    value that cannot be defaulted, or reshaping existing rows.

    Downgrades are refused rather than guessed at.
    """
    if from_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database is at schema version {from_version}, but this "
            f"version of stockroom only understands {SCHEMA_VERSION}. "
            "Upgrade the application rather than downgrading the database."
        )

    # 1 -> 2: accounts, sessions and requests. Purely additive; the schema
    # file has already created the tables and there is no data to reshape.

    if from_version < 3:
        # 2 -> 3: the audit log became a hash chain. Every pre-existing event
        # predates prev_hash/hash, so the chain is computed over them once,
        # in id order. Imported here rather than at module scope because
        # service imports db.
        from .service import rebuild_audit_chain

        rebuild_audit_chain(conn)

    # 3 -> 4: loans can name an individual unit, and a finished stocktake
    # records what it found. Both are handled elsewhere -- the column by
    # _ensure_columns, the table and index by schema.sql -- and there is
    # nothing to backfill: which camera body went out on a loan closed last
    # year is not recoverable from anything, so those rows honestly stay NULL.
    # The version still moves, so an older binary refuses this database
    # instead of quietly writing loans with no unit on them.

    # 4 -> 5: single sign-on. Nothing to do. The four new account columns are
    # added by _ensure_columns and their literal defaults are already right
    # for every existing row -- an account that predates SSO did arrive by
    # password, which is exactly what auth_source defaults to. The
    # saml_auth_request table and the sso_uid index come from schema.sql.


def _backfill_fts(conn: sqlite3.Connection) -> None:
    """Populate the search index for rows that predate it.

    Relevant when FTS5 was unavailable (or the index was rebuilt) while items
    were being added -- the triggers only cover changes made after the index
    exists.
    """
    n_items = conn.execute("SELECT COUNT(*) AS n FROM item").fetchone()["n"]
    n_index = conn.execute("SELECT COUNT(*) AS n FROM item_fts").fetchone()["n"]
    if n_items == n_index:
        return
    with transaction(conn):
        conn.execute("DELETE FROM item_fts")
        conn.execute(
            """
            INSERT INTO item_fts (rowid, name, description, barcode, location)
            SELECT id, name, description, COALESCE(barcode, ''),
                   unit || ' ' || shelf || ' ' || COALESCE(sub_location, '')
            FROM item
            """
        )


class BackupCorrupt(RuntimeError):
    """A snapshot was written but did not verify. Treat it as no backup."""


def backup(
    destination: Path, db_path: Path | str | None = None, *, verify: bool = True
) -> Path:
    """Write a consistent snapshot of the database to ``destination``.

    Uses SQLite's online backup API, which is safe to run while the service
    is live -- unlike copying the .db file, which can catch a torn WAL.

    ``verify`` re-opens the snapshot and runs ``PRAGMA integrity_check`` on
    it. That is not paranoia: a database corrupted by a failing SD card copies
    without complaint, so an unverified nightly job cheerfully produces thirty
    days of unusable backups and reports success every time. A snapshot that
    does not verify is deleted and :class:`BackupCorrupt` is raised, because a
    file that looks like a backup and is not is worse than no file at all.
    """
    conn = connect(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        with target:
            conn.backup(target)
    finally:
        target.close()

    if verify:
        problem = verify_file(destination)
        if problem is not None:
            destination.unlink(missing_ok=True)
            raise BackupCorrupt(f"{destination.name} failed verification: {problem}")
    return destination


def verify_file(path: Path) -> str | None:
    """Run SQLite's own integrity checks over a database file.

    Returns ``None`` when the file is sound, or a short description of the
    first problem found. Used on fresh snapshots and by ``stockroom doctor``.
    """
    if not path.exists():
        return "file does not exist"
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"cannot open: {exc}"
    try:
        result = probe.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            return str(result[0]) if result else "integrity_check returned nothing"
        violations = probe.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            return f"{len(violations)} foreign key violation(s)"
    except sqlite3.DatabaseError as exc:
        return f"unreadable: {exc}"
    finally:
        probe.close()
    return None
