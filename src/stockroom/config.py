"""Configuration.

Everything is a plain module-level default that can be overridden with an
environment variable, so the Pi's systemd unit can configure the service
without editing code and the test suite can point at a temp directory.

Environment variables (all optional):

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
"""

from __future__ import annotations

import os
from pathlib import Path

# <repo root>/src/stockroom/config.py -> <repo root>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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

BARCODE_PREFIX: str = os.environ.get("STOCKROOM_BARCODE_PREFIX", "CIS")
BARCODE_DIGITS: int = 6  # CIS-000142

# A burst of changes (a CSV import, rapid check-ins) should render the public
# site once, not once per row.
PUBLISH_DEBOUNCE_SECONDS: float = float(
    os.environ.get("STOCKROOM_PUBLISH_DEBOUNCE", "2.0")
)

# Number of nightly database snapshots to keep (see deploy/stockroom-backup).
BACKUP_KEEP: int = int(os.environ.get("STOCKROOM_BACKUP_KEEP", "30"))
