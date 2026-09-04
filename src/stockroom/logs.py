"""Getting the system log off the SD card.

RIT's Server Security Standard asks two things about logging that this
deployment cannot answer the same way a data centre would:

* **(3.5) at least two weeks** of authentication, privilege-escalation,
  account-change and job-start-up records. `deploy/harden-pi.sh` answers this
  by making the journal persistent -- on a stock Raspberry Pi OS install
  `Storage=auto` with no `/var/log/journal` means the journal is *volatile*
  and the whole thing is gone at the next reboot.
* **(3.7) mirrored in real time onto another secure server.** That wants a log
  server, and there isn't one.

This module is the honest partial answer to the second: once a night, export a
window of the journal and hand it to the same off-box targets the database
snapshot goes to. It is a night behind, so it is **not** 3.7 and must not be
initialled as such on the checklist -- see docs/its-registration.md. What it
does buy is the thing that actually kills this machine: when the SD card dies,
the log of what happened before it died is not on the card.

Two deliberate choices:

**The whole journal, not just this service's unit.** The elements the standard
lists -- authentication, privilege escalation, user additions, access-control
changes -- are sshd, sudo and systemd, not `stockroom`. Reading them needs the
service account in the `systemd-journal` group, which `harden-pi.sh` arranges;
without it `journalctl` quietly shows only that user's own entries and the
export looks like it worked.

**The window overlaps.** A nightly "last two days", kept thirty deep, survives
a night the Pi was switched off without leaving a hole. Exactly one day does
not.

What lands in the archive is the same class of information as the database
snapshot beside it -- email addresses appear in this application's own log
lines -- so it goes to the same private destination and no further. It does
not contain passwords: nothing here logs one, and
`tests/test_source_hygiene.py` is where that is kept true.
"""

from __future__ import annotations

import gzip
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import backup_targets, config

log = logging.getLogger(__name__)

# Long enough for a month of journal on a slow card, bounded so the nightly
# timer cannot wedge on a broken journalctl.
_TIMEOUT = 300


class LogExportError(RuntimeError):
    """The journal could not be read."""


def archive_name(when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{backup_targets.LOG_PREFIX}{stamp}.txt.gz"


def export(
    destination: Path | None = None,
    *,
    days: int | None = None,
    journalctl: str | None = None,
) -> Path:
    """Write a gzipped window of the journal, and return the file.

    Raises :class:`LogExportError` rather than leaving a truncated or empty
    archive: a zero-byte file that satisfies every existence-and-age check is
    the failure mode this whole module exists to avoid, and it is exactly what
    an unreadable journal produces if nobody looks at the exit code.
    """
    days = config.LOG_ARCHIVE_DAYS if days is None else days
    binary = journalctl or config.JOURNALCTL
    target = destination or (config.BACKUP_DIR / archive_name())
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            [binary, "--since", f"{days} days ago", "--no-pager",
             "--output", "short-iso"],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LogExportError(
            f"{binary!r} is not available, so there is no journal to archive"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LogExportError(f"journalctl timed out after {_TIMEOUT}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise LogExportError(
            f"journalctl failed: {detail[-1] if detail else exc}"
        ) from exc

    body = result.stdout
    if not body.strip():
        # Almost always the group membership: journalctl run by a user outside
        # `systemd-journal` reports success and prints that user's own (empty)
        # journal. Naming it here is the difference between a five-minute fix
        # and a month of empty archives nobody opened.
        raise LogExportError(
            f"the journal came back empty for the last {days} day(s). If this "
            "is the nightly job, the service account is probably not in the "
            "`systemd-journal` group -- re-run deploy/harden-pi.sh"
        )

    # Write beside the final name and rename, so an interrupted export never
    # leaves a short file wearing an archive's name. Same rule as
    # LocalDirTarget.store, for the same reason.
    partial = target.with_name(target.name + ".part")
    try:
        with gzip.open(partial, "wt", encoding="utf-8") as handle:
            handle.write(body)
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    log.info("archived %s of journal to %s", f"{days}d", target)
    return target


def prune(directory: Path | None = None, *, keep: int | None = None) -> list[Path]:
    """Drop the oldest local archives. Returns what was removed."""
    directory = directory or config.BACKUP_DIR
    keep = config.LOG_ARCHIVE_KEEP if keep is None else keep
    if keep <= 0 or not directory.is_dir():
        return []
    archives = sorted(directory.glob(backup_targets.LOG_GLOB))
    stale = archives[: max(0, len(archives) - keep)]
    for path in stale:
        path.unlink(missing_ok=True)
    return stale
