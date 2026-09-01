"""Backups that leave the machine, and the checks that notice when they stop.

The failure these guard against is not dramatic. It is a nightly timer that
stopped firing in March, or thirty snapshots of a database that was already
corrupt, discovered on the one day anybody needed them.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import textwrap
from pathlib import Path

import pytest

from stockroom import backup_targets, config, db, diagnostics, service
from stockroom.backup_targets import LocalDirTarget, RcloneTarget
from stockroom.service import Actor

SETUP = Actor("cli:test")


@pytest.fixture
def snapshot(conn, actor, item, tmp_path):
    """A real, verified snapshot of a database with something in it."""
    return db.backup(tmp_path / "snapshots" / "stockroom-2026-08-31T000000Z.db")


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_a_good_snapshot_verifies(conn, actor, item, tmp_path):
    target = db.backup(tmp_path / "stockroom-good.db")
    assert target.exists()
    assert db.verify_file(target) is None


def test_the_snapshot_really_contains_the_data(snapshot):
    probe = sqlite3.connect(snapshot)
    probe.row_factory = sqlite3.Row
    try:
        names = [r["name"] for r in probe.execute("SELECT name FROM item")]
    finally:
        probe.close()
    assert names == ["SanDisk 64GB SD Card"]


def test_a_corrupt_file_does_not_verify(tmp_path):
    """The case that matters: a file that exists and is useless."""
    broken = tmp_path / "corrupt.db"
    broken.write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
    assert db.verify_file(broken) is not None


def test_verify_reports_a_missing_file_rather_than_raising(tmp_path):
    assert db.verify_file(tmp_path / "nope.db") == "file does not exist"


def test_a_snapshot_that_fails_verification_is_deleted(conn, actor, item,
                                                       tmp_path, monkeypatch):
    """A file that looks like a backup and is not is worse than no file.

    If it survived, the rotation would count it, `doctor` would call the
    backups fresh, and the bad snapshot would be the one restored.
    """
    monkeypatch.setattr(db, "verify_file", lambda path: "simulated corruption")
    target = tmp_path / "stockroom-doomed.db"

    with pytest.raises(db.BackupCorrupt, match="simulated corruption"):
        db.backup(target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# a second local copy
# ---------------------------------------------------------------------------


def test_local_copy_target_copies_the_snapshot(snapshot, tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    LocalDirTarget(usb).store(snapshot)
    assert (usb / snapshot.name).read_bytes() == snapshot.read_bytes()


def test_local_copy_target_prunes_to_its_own_limit(snapshot, tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    for n in range(5):
        (usb / f"stockroom-2026-01-{n + 1:02d}T000000Z.db").write_text("old")

    LocalDirTarget(usb, keep=3).store(snapshot)

    remaining = sorted(p.name for p in usb.glob("stockroom-*.db"))
    assert len(remaining) == 3
    assert snapshot.name in remaining          # the newest always survives
    assert "stockroom-2026-01-01T000000Z.db" not in remaining


def test_an_unmounted_drive_says_so(snapshot, tmp_path):
    """The overwhelmingly common cause, and the one worth naming."""
    target = LocalDirTarget(tmp_path / "not-mounted")
    with pytest.raises(RuntimeError, match="mounted"):
        target.store(snapshot)


def test_an_off_box_copy_that_does_not_verify_is_refused(snapshot, tmp_path,
                                                         monkeypatch):
    """Same rule the local snapshot already follows.

    db.backup() deletes a snapshot that fails verification, on the grounds
    that a file which looks like a backup and is not is worse than no file.
    The copy on the stick was then made with a bare copy2 -- unchecked and not
    atomic -- so a drive pulled mid-write left a truncated file that passes
    every existence-and-age check doctor makes.
    """
    usb = tmp_path / "usb"
    usb.mkdir()
    monkeypatch.setattr(db, "verify_file", lambda path: "disk image is malformed")

    with pytest.raises(RuntimeError, match="did not verify"):
        LocalDirTarget(usb).store(snapshot)

    assert list(usb.iterdir()) == [], \
        "a copy that failed to verify was left behind wearing a backup's name"


def test_a_partial_copy_never_wears_a_backups_name(snapshot, tmp_path, monkeypatch):
    """The rename is what makes it atomic: readers see all of it or none."""
    usb = tmp_path / "usb"
    usb.mkdir()
    seen = []
    real_copy = backup_targets.shutil.copy2

    def watching_copy(src, dst):
        seen.append(Path(dst).name)
        return real_copy(src, dst)

    monkeypatch.setattr(backup_targets.shutil, "copy2", watching_copy)
    LocalDirTarget(usb).store(snapshot)

    assert seen == [snapshot.name + ".part"], \
        "the copy was written straight to its final name"
    assert (usb / snapshot.name).read_bytes() == snapshot.read_bytes()
    assert not list(usb.glob("*.part"))


def test_a_copy_directory_the_job_cannot_write_to_is_named(temp_env, monkeypatch,
                                                           tmp_path):
    """The nightly unit runs under ProtectSystem=strict.

    A copy directory outside /var/lib/stockroom is read-only to it, while the
    same command by hand works -- so "empty" was the only clue, and it points
    at the wrong problem entirely.
    """
    usb = tmp_path / "usb"
    usb.mkdir()
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", usb)
    usb.chmod(0o555)
    try:
        check = diagnostics.check_offsite_backups(skip_remote=True)
    finally:
        usb.chmod(0o755)

    assert not check.ok
    assert "not writable" in check.detail
    assert "ReadWritePaths" in check.detail, \
        "the fix is a systemd sandbox setting; say so"


# ---------------------------------------------------------------------------
# rclone
#
# Tested against a stub on PATH, never against a real Google account: these
# assert the argv this code builds, which is the part that can be wrong.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_rclone(tmp_path):
    """A stand-in rclone backed by a directory, plus a log of every call."""
    drive = tmp_path / "drive"
    drive.mkdir()
    log = tmp_path / "calls.log"
    binary = tmp_path / "rclone"
    binary.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> {log}
        case "$1" in
          copyto) cp "$2" "{drive}/$(basename "$3")" ;;
          lsf) ls "{drive}" 2>/dev/null | grep '^stockroom-.*\\.db$' || true ;;
          deletefile) rm -f "{drive}/$(basename "$2")" ;;
          *) echo "unsupported: $1" >&2; exit 1 ;;
        esac
        """))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return type("Fake", (), {"binary": str(binary), "drive": drive, "log": log})


def _calls(fake) -> list[str]:
    return fake.log.read_text().splitlines() if fake.log.exists() else []


def test_rclone_target_uploads_the_snapshot(snapshot, fake_rclone):
    RcloneTarget("gdrive:stockroom", binary=fake_rclone.binary).store(snapshot)

    assert (fake_rclone.drive / snapshot.name).exists()
    assert _calls(fake_rclone)[0] == (
        f"copyto {snapshot} gdrive:stockroom/{snapshot.name}"
    )


def test_rclone_target_lists_what_is_already_there(snapshot, fake_rclone):
    target = RcloneTarget("gdrive:stockroom", binary=fake_rclone.binary)
    target.store(snapshot)
    assert target.existing() == [snapshot.name]


def test_rclone_target_prunes_the_remote(snapshot, fake_rclone):
    for n in range(4):
        (fake_rclone.drive / f"stockroom-2026-01-{n + 1:02d}T000000Z.db").write_text("x")

    RcloneTarget("gdrive:stockroom", binary=fake_rclone.binary, keep=2).store(snapshot)

    left = sorted(p.name for p in fake_rclone.drive.glob("stockroom-*.db"))
    assert len(left) == 2
    assert snapshot.name in left
    assert any(c.startswith("deletefile") for c in _calls(fake_rclone))


def test_a_missing_rclone_binary_says_how_to_install_it(snapshot):
    target = RcloneTarget("gdrive:stockroom", binary="/nonexistent/rclone")
    with pytest.raises(RuntimeError, match="apt install rclone"):
        target.store(snapshot)


def test_an_rclone_failure_surfaces_its_own_error(snapshot, tmp_path):
    binary = tmp_path / "rclone-broken"
    binary.write_text("#!/bin/bash\necho 'Failed to copy: quota exceeded' >&2\nexit 1\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    target = RcloneTarget("gdrive:stockroom", binary=str(binary))
    with pytest.raises(RuntimeError, match="quota exceeded"):
        target.store(snapshot)


def test_an_unconfigured_remote_is_refused_before_any_work(snapshot):
    with pytest.raises(RuntimeError, match="not configured"):
        RcloneTarget("").store(snapshot)


# ---------------------------------------------------------------------------
# fan-out
# ---------------------------------------------------------------------------


def test_one_failing_target_does_not_stop_the_others(snapshot, tmp_path):
    """An unplugged USB stick must not cost you the Drive copy."""
    usb = tmp_path / "usb"
    usb.mkdir()
    failures = backup_targets.copy_to_targets(
        snapshot,
        [LocalDirTarget(tmp_path / "missing"), LocalDirTarget(usb)],
    )

    assert (usb / snapshot.name).exists()          # the good one still ran
    assert len(failures) == 1
    assert failures[0].target == "local-copy"


def test_nothing_is_configured_by_default(temp_env):
    assert backup_targets.configured_targets() == []


def test_targets_are_built_from_configuration(temp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_REMOTE", "gdrive:stockroom")
    assert [t.name for t in backup_targets.configured_targets()] == [
        "local-copy", "rclone",
    ]


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


def test_every_check_runs_against_an_empty_database(conn):
    """`doctor` must work on a Pi that was set up five minutes ago."""
    report = diagnostics.run_all(conn, skip_remote=True)
    assert len(report.checks) == 11
    assert all(isinstance(c.detail, str) and c.detail for c in report.checks)


def test_a_healthy_system_passes(conn, actor, item, tmp_path, monkeypatch):
    from stockroom import accounts
    from stockroom.publish.render import render_site

    accounts.register(conn, first_name="Ada", last_name="Admin",
                      email="ada@rit.edu", password="glass onion tuesday lamp",
                      role="admin", status="active", actor=SETUP)
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.backup(config.BACKUP_DIR / "stockroom-now.db")
    config.PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in render_site(conn).items():
        (config.PUBLISH_DIR / name).write_text(body)

    report = diagnostics.run_all(conn, skip_remote=True)
    assert report.ok, [str(c.name) + ": " + c.detail for c in report.failures]


def test_a_tampered_audit_log_fails_the_health_check(conn, actor, item):
    conn.execute("UPDATE event SET summary = 'rewritten' WHERE id = 1")
    conn.commit()
    report = diagnostics.run_all(conn, skip_remote=True)
    assert "audit chain" in [c.name for c in report.failures]


def test_missing_backups_are_a_failure_not_a_warning(conn):
    check = diagnostics.check_backups(conn)
    assert check.status == diagnostics.FAIL


def test_no_off_box_backup_is_a_warning(conn, temp_env):
    """Not configured is a real gap, but it is not a broken machine."""
    check = diagnostics.check_offsite_backups()
    assert check.status == diagnostics.WARN
    assert "SD card" in check.detail


def test_a_search_index_out_of_step_is_reported(conn, actor, item):
    if not db.fts_enabled(conn):
        pytest.skip("this SQLite build has no FTS5")
    conn.execute("INSERT INTO item_fts (rowid, name) VALUES (9999, 'ghost')")
    conn.commit()
    assert diagnostics.check_search_index(conn).status == diagnostics.FAIL


def test_losing_the_last_administrator_is_reported(conn):
    assert diagnostics.check_administrators(conn).status == diagnostics.FAIL


def test_a_broken_check_becomes_a_finding_rather_than_a_crash(conn, monkeypatch):
    def explode(*args, **kwargs):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(diagnostics, "check_disk_space", explode)
    report = diagnostics.run_all(conn, skip_remote=True)
    assert "disk space" in [c.name for c in report.failures]
    assert len(report.checks) == 11
