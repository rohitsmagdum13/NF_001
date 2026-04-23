"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from newsfeed import db


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Iterator[Path]:
    """Provide a temp SQLite path and reset the db module's cached engine."""
    db_path = tmp_path / "test_pipeline.db"
    db.init_db(db_path)
    yield db_path
    db._engine = None  # reset for next test
    db._Session = None
