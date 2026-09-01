"""Shared fixtures.

Every test gets a fresh database and publish directory in a temp dir. The
config module is patched *before* the app is imported so that the module-level
paths (config.DB_PATH, config.PUBLISH_DIR) point at the temp location.
"""

from __future__ import annotations

import pytest

from stockroom import config, db, security, service
from stockroom.service import Actor


@pytest.fixture(autouse=True)
def temp_env(tmp_path, monkeypatch):
    """Point the whole application at a throwaway directory."""
    # Process-global and deliberately not in the database, so unlike every
    # other bit of state here it survives the temp directory. A test that
    # tripped a lockout would otherwise silently suppress the audit event a
    # later test asserts on.
    security.lockout_log_throttle.reset()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "stockroom.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "data" / "backups")
    monkeypatch.setattr(config, "PUBLISH_DIR", tmp_path / "publish")
    monkeypatch.setattr(config, "GITHUB_PAGES_DIR", None)
    monkeypatch.setattr(config, "PUBLIC_SHOW_BORROWERS", False)
    # No background publishing during tests unless a test asks for it.
    service.set_change_listener(None)
    yield tmp_path
    db.close_all()


@pytest.fixture
def conn(temp_env):
    return db.init_db()


@pytest.fixture
def actor():
    return Actor("Test Operator", "operator@rit.edu")


@pytest.fixture
def item(conn, actor):
    """One item with ten units, low-stock threshold 2."""
    return service.create_item(
        conn, actor=actor, name="SanDisk 64GB SD Card",
        description="Class 10 UHS-I", quantity=10,
        unit="Unit B", shelf="Shelf 3", sub_location="Bin 12",
        min_quantity=2,
    )


@pytest.fixture
def person(conn, actor):
    return service.create_person(
        conn, actor=actor, name="Alice Nguyen", email="alice@rit.edu"
    )
