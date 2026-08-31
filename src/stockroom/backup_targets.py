"""Where a database snapshot goes once it has been written.

The nightly job already produced a verified snapshot on the Pi. The problem
that leaves is stated plainly in docs/security.md: those snapshots live on the
same SD card as the database they protect, and the SD card is the component
most likely to fail. A backup that dies with the machine is not a backup.

A target takes one snapshot file and puts a copy somewhere else. This mirrors
:mod:`stockroom.publish.publishers` deliberately -- same Protocol shape, same
opt-in ``configured_targets()``, same rule about credentials:

    A target never handles credentials itself. The rclone target runs the
    ``rclone`` binary and lets the operator's existing ``rclone.conf``
    authenticate, exactly as the GitHub Pages publisher runs ``git`` and lets
    the checkout authenticate. No token ever reaches this codebase.

Unlike publishing, a failure here is worth surfacing. A publish that fails is
retried on the next change a minute later; a backup that silently fails to
leave the building is not noticed until the day it is needed. Targets raise,
:func:`copy_to_targets` collects the failures without letting one stop the
others, and the caller reports them.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import config

log = logging.getLogger(__name__)

# rclone talks to the network. Generous, because a first upload over a slow
# campus link is not a hang, but bounded, because the nightly timer must not
# wedge forever.
_RCLONE_TIMEOUT = 600


@runtime_checkable
class BackupTarget(Protocol):
    """Delivers one snapshot file to one destination."""

    name: str

    def store(self, snapshot: Path) -> None: ...

    def existing(self) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class TargetError:
    """One target's failure, kept so every target still gets its turn."""

    target: str
    error: Exception

    def __str__(self) -> str:
        return f"{self.target}: {self.error}"


class LocalDirTarget:
    """Copy the snapshot into another directory -- typically a USB stick.

    The cheapest possible off-card backup, and the one that still works when
    the campus network does not. Rotation is separate from the primary
    directory's: a stick that is only plugged in occasionally should keep what
    it has rather than pruning to match a schedule it never saw.
    """

    name = "local-copy"

    def __init__(self, directory: Path | None = None, keep: int | None = None) -> None:
        self.directory = Path(directory) if directory else config.BACKUP_COPY_DIR
        self.keep = config.BACKUP_KEEP if keep is None else keep

    def store(self, snapshot: Path) -> None:
        if self.directory is None:
            raise RuntimeError("BACKUP_COPY_DIR is not configured.")
        if not self.directory.is_dir():
            # Almost always an unmounted stick rather than a typo. Say so:
            # "no such directory" sends people looking for the wrong problem.
            raise RuntimeError(
                f"{self.directory} is not a directory -- is the drive mounted?"
            )
        # copy2 preserves mtime, which is what "how old is my newest backup?"
        # reads in `stockroom doctor`.
        shutil.copy2(snapshot, self.directory / snapshot.name)
        self._prune()
        log.info("copied %s to %s", snapshot.name, self.directory)

    def existing(self) -> list[str]:
        if self.directory is None or not self.directory.is_dir():
            return []
        return sorted(p.name for p in self.directory.glob("stockroom-*.db"))

    def _prune(self) -> None:
        if self.keep <= 0 or self.directory is None:
            return
        snapshots = sorted(self.directory.glob("stockroom-*.db"))
        for stale in snapshots[: max(0, len(snapshots) - self.keep)]:
            stale.unlink(missing_ok=True)


class RcloneTarget:
    """Upload the snapshot to an rclone remote (Google Drive, and anything else).

    rclone is one static binary from apt with its own OAuth flow and token
    refresh, which is why it is here instead of a Google SDK: this application
    keeps five runtime dependencies and none of them handle other people's
    refresh tokens. Set up once, as the service user::

        sudo -u stockroom rclone config

    then point ``STOCKROOM_BACKUP_REMOTE`` at the result, e.g.
    ``gdrive:stockroom-backups``.
    """

    name = "rclone"

    def __init__(
        self,
        remote: str | None = None,
        *,
        binary: str | None = None,
        keep: int | None = None,
    ) -> None:
        self.remote = (remote if remote is not None else config.BACKUP_REMOTE).strip()
        self.binary = binary or config.RCLONE
        self.keep = config.BACKUP_REMOTE_KEEP if keep is None else keep

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True, text=True, check=True,
                timeout=_RCLONE_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{self.binary!r} is not installed. On the Pi: "
                "sudo apt install rclone"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"rclone {args[0]} timed out after {_RCLONE_TIMEOUT}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            # rclone puts the useful line on stderr; the exception's own repr
            # does not include it, and without it every failure looks the same.
            detail = (exc.stderr or exc.stdout or "").strip().splitlines()
            raise RuntimeError(
                f"rclone {args[0]} failed: {detail[-1] if detail else exc}"
            ) from exc

    def store(self, snapshot: Path) -> None:
        if not self.remote:
            raise RuntimeError("BACKUP_REMOTE is not configured.")
        self._run("copyto", str(snapshot), f"{self.remote}/{snapshot.name}")
        self._prune()
        log.info("uploaded %s to %s", snapshot.name, self.remote)

    def existing(self) -> list[str]:
        if not self.remote:
            return []
        result = self._run("lsf", self.remote, "--include", "stockroom-*.db")
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _prune(self) -> None:
        if self.keep <= 0:
            return
        snapshots = self.existing()
        for stale in snapshots[: max(0, len(snapshots) - self.keep)]:
            self._run("deletefile", f"{self.remote}/{stale}")
            log.info("pruned %s from %s", stale, self.remote)


def configured_targets() -> list[BackupTarget]:
    """The off-box targets enabled by the current configuration.

    Empty by default. A stockroom that has not been told where to put a second
    copy does not get one silently invented for it.
    """
    targets: list[BackupTarget] = []
    if config.BACKUP_COPY_DIR is not None:
        targets.append(LocalDirTarget())
    if config.BACKUP_REMOTE:
        targets.append(RcloneTarget())
    return targets


def copy_to_targets(
    snapshot: Path, targets: list[BackupTarget] | None = None
) -> list[TargetError]:
    """Send one snapshot to every configured target.

    Returns the failures rather than raising them: one unmounted USB stick
    must not stop the Drive upload, and neither must undo the local snapshot
    that already succeeded. The caller decides what a failure means -- for the
    CLI that is a non-zero exit code, so systemd records it.
    """
    failures: list[TargetError] = []
    for target in configured_targets() if targets is None else targets:
        try:
            target.store(snapshot)
        except Exception as exc:
            log.error("backup target %r failed: %s", target.name, exc)
            failures.append(TargetError(target.name, exc))
    return failures
