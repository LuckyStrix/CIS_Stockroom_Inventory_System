"""Generation and distribution of the public, read-only inventory page.

Split three ways:

* :mod:`~stockroom.publish.render` -- turn the database into an ``index.html``
  and an ``inventory.json``.
* :mod:`~stockroom.publish.publishers` -- put those files somewhere people can
  reach them (a local directory, a GitHub Pages checkout).
* :mod:`~stockroom.publish.worker` -- notice that something changed and
  rebuild, debounced, off the request thread.
"""

from .render import render_site, render_json  # noqa: F401
from .publishers import GitHubPagesPublisher, LocalPublisher, Publisher  # noqa: F401
from .worker import PublishWorker, install, publish_now  # noqa: F401
