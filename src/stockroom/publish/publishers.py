"""Where the rendered public files get delivered.

A publisher takes ``{filename: contents}`` from
:func:`~stockroom.publish.render.render_site` and puts it somewhere. Adding a
destination later (an S3 bucket, a departmental web server, a Google Sheet)
means writing one class with a ``publish`` method and appending it to
:func:`configured_publishers` -- no other module changes.

Every publisher is expected to fail loudly but harmlessly: the worker logs
the failure and retries on the next change. A publishing problem must never
prevent someone from checking a camera out.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config

log = logging.getLogger(__name__)


@runtime_checkable
class Publisher(Protocol):
    """Delivers rendered files to one destination."""

    name: str

    def publish(self, files: dict[str, str]) -> None: ...


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a reader never sees a partial page.

    The rename is atomic within a filesystem, which means the web server
    either serves the old complete page or the new complete page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # mkstemp creates 0600. These files are meant to be read by a web
        # server -- and, on the Pi, by a different user than the one running
        # the service -- so widen to the usual 0644 before publishing them.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class LocalPublisher:
    """Write the site into a directory that the Pi serves at ``/public``.

    Always enabled. This is the copy that works with no network, no accounts
    and no credentials, and it is what the app itself serves.
    """

    name = "local"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else config.PUBLISH_DIR

    def publish(self, files: dict[str, str]) -> None:
        for filename, contents in files.items():
            _write_atomic(self.directory / filename, contents)
        log.info("published %d file(s) to %s", len(files), self.directory)


class GitHubPagesPublisher:
    """Mirror the site into a local git checkout and push it.

    Opt-in: set ``STOCKROOM_GITHUB_PAGES_DIR`` to a clone of the Pages repo
    that already has push credentials (a deploy key, or a token in the
    remote URL). This publisher never handles credentials itself -- it just
    runs ``git`` and lets the existing checkout authenticate.

    A no-op commit (nothing actually changed) is detected and skipped rather
    than producing an empty commit on every render.
    """

    name = "github-pages"

    def __init__(
        self, directory: Path | None = None, branch: str | None = None
    ) -> None:
        self.directory = Path(directory) if directory else config.GITHUB_PAGES_DIR
        self.branch = branch or config.GITHUB_PAGES_BRANCH

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.directory), *args],
            capture_output=True, text=True, check=check, timeout=120,
        )

    def publish(self, files: dict[str, str]) -> None:
        if self.directory is None:
            raise RuntimeError("GITHUB_PAGES_DIR is not configured.")
        if not (self.directory / ".git").exists():
            raise RuntimeError(f"{self.directory} is not a git checkout.")

        for filename, contents in files.items():
            _write_atomic(self.directory / filename, contents)

        self._git("add", *files.keys())
        # --quiet + exit code 1 means "there are staged changes".
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            log.info("github-pages: no content change, nothing to push")
            return

        self._git("commit", "-m", "Update stockroom inventory")
        self._git("push", "origin", f"HEAD:{self.branch}")
        log.info("github-pages: pushed to %s", self.branch)


def configured_publishers() -> list[Publisher]:
    """The publishers enabled by the current configuration."""
    publishers: list[Publisher] = [LocalPublisher()]
    if config.GITHUB_PAGES_DIR is not None:
        publishers.append(GitHubPagesPublisher())
    return publishers
