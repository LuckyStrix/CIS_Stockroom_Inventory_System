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

SCHEMA_VERSION = 2

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
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if not hasattr(_local, "conns"):
        _local.conns = cache

    conn = cache.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=5.0)
        _configure(conn)
        cache[key] = conn
    return conn


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
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
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


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create or upgrade the schema. Safe to run on every start.

    Every statement is ``IF NOT EXISTS`` or ``DROP``-then-``CREATE`` for
    views and triggers, so this is idempotent -- the systemd unit runs it at
    boot so a fresh Pi comes up with a working database and no manual step.
    """
    conn = connect(db_path)

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

    Version 1 -> 2 (accounts, sessions, requests) is purely additive, so there
    is nothing to do beyond recording the new version. Downgrades are refused
    rather than guessed at.
    """
    if from_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database is at schema version {from_version}, but this "
            f"version of stockroom only understands {SCHEMA_VERSION}. "
            "Upgrade the application rather than downgrading the database."
        )
    # 1 -> 2: additive only; the schema file has already created the new
    # tables. Future migrations append their steps here.


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


def backup(destination: Path, db_path: Path | str | None = None) -> Path:
    """Write a consistent snapshot of the database to ``destination``.

    Uses SQLite's online backup API, which is safe to run while the service
    is live -- unlike copying the .db file, which can catch a torn WAL.
    """
    conn = connect(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        with target:
            conn.backup(target)
    finally:
        target.close()
    return destination
