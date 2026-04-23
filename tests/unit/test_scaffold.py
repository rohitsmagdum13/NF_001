"""Smoke tests proving the scaffold is wired correctly."""

from __future__ import annotations

from pathlib import Path

import pytest

from newsfeed.config import load_settings
from newsfeed.db import Candidate, SeenHash, get_session, init_db


@pytest.mark.unit
def test_settings_load_from_default_yaml() -> None:
    settings = load_settings()
    assert settings.pipeline.lookback_days >= 1
    assert "Water Reform" in settings.sections
    assert "National" in settings.jurisdictions
    assert set(settings.content_types) == {
        "new_plan",
        "media_release",
        "weekly_report",
        "bullet_list",
        "simple_update",
        "gazette_bundle",
    }


@pytest.mark.unit
def test_settings_paths_are_resolved_absolute() -> None:
    settings = load_settings()
    for p in (
        settings.paths.inbox,
        settings.paths.cache,
        settings.paths.outputs,
        settings.paths.db,
        settings.paths.reference,
    ):
        assert p.is_absolute(), f"path not absolute: {p}"


@pytest.mark.unit
def test_db_init_creates_tables_and_roundtrip(tmp_db_path: Path) -> None:
    init_db(tmp_db_path)
    with get_session() as session:
        session.add(
            Candidate(
                source_id="test_source",
                source_type="url",
                url="https://example.com/a",
                title="Example",
                hash="abc123",
            )
        )
        session.add(SeenHash(hash="abc123", source_id="test_source"))

    with get_session() as session:
        row = session.query(Candidate).filter_by(hash="abc123").one()
        assert row.status == "new"
        assert row.source_type == "url"
