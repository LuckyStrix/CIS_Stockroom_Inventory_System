"""Rebuild the public page when the inventory changes.

Two things matter here:

* **Debouncing.** A CSV import of 200 rows fires 200 change notifications.
  Rendering 200 times would be pointless work and would hammer git if the
  Pages publisher is on, so notifications inside a short window collapse into
  one render.

* **Isolation from the request.** Rendering (and especially ``git push``)
  happens on a background thread. A checkout returns as soon as it is
  committed; it never waits on the network, and a publisher that raises does
  not turn a successful checkout into an error page.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Sequence

from .. import config, db, service
from .publishers import Publisher, configured_publishers
from .render import render_site

log = logging.getLogger(__name__)


class PublishWorker:
    """Coalesces change notifications into debounced background renders."""

    def __init__(
        self,
        publishers: Sequence[Publisher] | None = None,
        *,
        debounce: float | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self.publishers = list(publishers) if publishers is not None else configured_publishers()
        self.debounce = config.PUBLISH_DEBOUNCE_SECONDS if debounce is None else debounce
        # Captured at construction: the worker opens its own connection on a
        # background thread, and must open the SAME database the app is using
        # rather than whatever the global default happens to be.
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_error: Exception | None = None
        self.publish_count = 0

    # -- notification ------------------------------------------------------
    def notify(self) -> None:
        """Record that something changed; render once the dust settles.

        Each notification restarts the timer, so a burst of changes produces
        a single render ``debounce`` seconds after the last one.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self) -> None:
        try:
            self.publish()
        except Exception as exc:  # pragma: no cover - logged, never raised
            self.last_error = exc
            log.exception("publish failed")

    # -- rendering ---------------------------------------------------------
    def publish(self) -> dict[str, str]:
        """Render and deliver right now, on the calling thread.

        A failing publisher is logged and skipped so that one broken
        destination (an offline git remote) cannot stop the others (the local
        page the stockroom actually reads).
        """
        # This runs on a worker thread, so it needs its own connection --
        # db.connect() is thread-local, which is exactly what we want.
        conn = db.connect(self.db_path)
        files = render_site(conn)
        for publisher in self.publishers:
            try:
                publisher.publish(files)
            except Exception as exc:
                self.last_error = exc
                # One readable line by default; the full traceback only when
                # debug logging is on. A publisher failing is an operational
                # condition (remote down, directory not writable), not a crash,
                # and it must not look like one in the journal or the CLI.
                log.error(
                    "publisher %r failed: %s",
                    getattr(publisher, "name", publisher),
                    exc,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
        self.publish_count += 1
        return files

    def flush(self, timeout: float = 30.0) -> None:
        """Run any pending render immediately. Used by tests and CLI exit."""
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            self.publish()

    def shutdown(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


_worker: PublishWorker | None = None


def install(
    worker: PublishWorker | None = None, *, db_path: Path | str | None = None
) -> PublishWorker:
    """Create the worker and wire it to the service layer's change hook."""
    global _worker
    _worker = worker or PublishWorker(db_path=db_path)
    service.set_change_listener(_worker.notify)
    return _worker


def get_worker() -> PublishWorker | None:
    return _worker


def publish_now(db_path: Path | str | None = None) -> dict[str, str]:
    """Render and deliver synchronously, whether or not a worker is installed."""
    worker = _worker or PublishWorker(db_path=db_path)
    return worker.publish()
