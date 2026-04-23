"""Unit tests for Stage 3 — deduplication / TTL pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, SeenHash, get_session
from newsfeed.stage3_dedup import run


def _add_seen_hash(hash_val: str, age_days: float) -> None:
    ts = datetime.now(UTC) - timedelta(days=age_days)
    with get_session() as session:
        session.add(SeenHash(hash=hash_val, first_seen_at=ts))


def _add_candidate(url: str, hash_val: str, status: str = "new") -> None:
    with get_session() as session:
        session.add(
            Candidate(
                source_id="test_source",
                source_type="url",
                url=url,
                hash=hash_val,
                status=status,
            )
        )


@pytest.mark.unit
def test_run_prunes_hashes_beyond_ttl(tmp_db_path: Path) -> None:
    settings = get_settings()
    ttl = settings.pipeline.lookback_days * 4

    _add_seen_hash("old_hash_beyond_ttl", age_days=ttl + 1)
    _add_seen_hash("recent_hash_within_ttl", age_days=ttl - 1)

    counts = run()

    assert counts["pruned_hashes"] == 1

    with get_session() as session:
        remaining = [r for (r,) in session.execute(select(SeenHash.hash))]

    assert "old_hash_beyond_ttl" not in remaining
    assert "recent_hash_within_ttl" in remaining


@pytest.mark.unit
def test_run_keeps_hashes_within_ttl(tmp_db_path: Path) -> None:
    _add_seen_hash("keep_this_hash", age_days=1)

    counts = run()

    assert counts["pruned_hashes"] == 0


@pytest.mark.unit
def test_run_reports_candidates_new_count(tmp_db_path: Path) -> None:
    _add_candidate("https://example.com/a1", "hash_new_a1", status="new")
    _add_candidate("https://example.com/a2", "hash_new_a2", status="new")
    _add_candidate("https://example.com/a3", "hash_filtered_a3", status="filtered")

    counts = run()

    assert counts["candidates_new"] == 2


@pytest.mark.unit
def test_run_with_empty_db(tmp_db_path: Path) -> None:
    counts = run()

    assert counts["pruned_hashes"] == 0
    assert counts["candidates_new"] == 0


@pytest.mark.unit
def test_run_ttl_days_matches_config(tmp_db_path: Path) -> None:
    settings = get_settings()
    expected_ttl = settings.pipeline.lookback_days * 4

    counts = run()

    assert counts["ttl_days"] == expected_ttl


@pytest.mark.unit
def test_run_prunes_multiple_old_hashes(tmp_db_path: Path) -> None:
    settings = get_settings()
    ttl = settings.pipeline.lookback_days * 4

    for i in range(5):
        _add_seen_hash(f"old_hash_{i}", age_days=ttl + i + 1)
    _add_seen_hash("keep_hash", age_days=1)

    counts = run()

    assert counts["pruned_hashes"] == 5

    with get_session() as session:
        remaining_count = session.execute(
            select(SeenHash).where(SeenHash.hash == "keep_hash")
        ).scalar_one_or_none()
    assert remaining_count is not None
