"""Unit tests for Stage 4 — fetch full content."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select as sa_select

from newsfeed.db import Candidate, ContextBundle, get_session
from newsfeed.schemas import SourceEntry, SourceRegistry
from newsfeed.stage4_fetch import (
    _cache_path,
    _extract_text,
    _find_sublinks,
    _process_candidate,
    fetch_all,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
SAMPLE_ARTICLE_HTML = (FIXTURES / "sample_article.html").read_text(encoding="utf-8")

_SOURCE = SourceEntry(
    source_id="water_reform__national__abc123def4",
    source_type="url",
    url="https://example.com/news",
    section="Water Reform",
    jurisdiction="National",
)
_SOURCE_MAP = {_SOURCE.source_id: _SOURCE}


def _add_candidate(
    tmp_db_path: Path,
    *,
    url: str = "https://example.com/article",
    source_type: str = "url",
    status: str = "new",
    source_id: str = "water_reform__national__abc123def4",
) -> int:
    with get_session() as session:
        c = Candidate(
            source_id=source_id,
            source_type=source_type,
            url=url,
            title="Test Article Title",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint="Water Reform",
            jurisdiction_hint="National",
            hash=f"hash_{url}",
            status=status,
        )
        session.add(c)
        session.flush()
        return int(c.id)


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_text_returns_prose_content() -> None:
    text = _extract_text(SAMPLE_ARTICLE_HTML)
    assert "water treatment" in text.lower() or "infrastructure" in text.lower()
    assert len(text) > 50


@pytest.mark.unit
def test_extract_text_handles_empty_html() -> None:
    text = _extract_text("<html><body></body></html>")
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# _find_sublinks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_sublinks_matches_pdf_pattern() -> None:
    links = _find_sublinks(SAMPLE_ARTICLE_HTML, "https://example.com/news/article", [r"\.pdf$"])
    assert any(link.endswith(".pdf") for link in links)


@pytest.mark.unit
def test_find_sublinks_matches_ministers_pattern() -> None:
    links = _find_sublinks(
        SAMPLE_ARTICLE_HTML, "https://example.com/news/article", [r"/ministers?/"]
    )
    assert any("ministers" in link for link in links)


@pytest.mark.unit
def test_find_sublinks_excludes_external_links() -> None:
    links = _find_sublinks(SAMPLE_ARTICLE_HTML, "https://example.com/news/article", [r".*"])
    assert not any("external-site.com" in link for link in links)


@pytest.mark.unit
def test_find_sublinks_returns_empty_for_no_match() -> None:
    links = _find_sublinks(
        SAMPLE_ARTICLE_HTML, "https://example.com/news/article", [r"/never-matches/"]
    )
    assert links == []


# ---------------------------------------------------------------------------
# _cache_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cache_path_is_deterministic(tmp_path: Path) -> None:
    p1 = _cache_path(tmp_path, "https://example.com/article")
    p2 = _cache_path(tmp_path, "https://example.com/article")
    assert p1 == p2


@pytest.mark.unit
def test_cache_path_differs_for_different_urls(tmp_path: Path) -> None:
    p1 = _cache_path(tmp_path, "https://example.com/a")
    p2 = _cache_path(tmp_path, "https://example.com/b")
    assert p1 != p2


# ---------------------------------------------------------------------------
# _process_candidate (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_process_candidate_fetches_and_creates_bundle(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    cid = _add_candidate(tmp_db_path)
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        assert candidate is not None

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, text=SAMPLE_ARTICLE_HTML)
    sem = asyncio.Semaphore(1)
    source_map = {_SOURCE.source_id: _SOURCE}

    status = await _process_candidate(candidate, source_map, client, sem, tmp_path)

    assert status == "fetched"

    with get_session() as session:
        bundle = session.execute(
            sa_select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one_or_none()

    assert bundle is not None
    assert bundle.main_text and len(bundle.main_text) > 20
    assert bundle.metadata_json is not None
    meta = json.loads(bundle.metadata_json)
    assert meta["section_hint"] == "Water Reform"


@pytest.mark.unit
async def test_process_candidate_returns_filtered_on_http_failure(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    cid = _add_candidate(tmp_db_path, url="https://example.com/unreachable")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        assert candidate is not None

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(404, text="Not found")
    sem = asyncio.Semaphore(1)
    source_map: dict[str, SourceEntry] = {}

    status = await _process_candidate(candidate, source_map, client, sem, tmp_path)

    assert status == "filtered"


@pytest.mark.unit
async def test_process_candidate_uses_cache_on_second_call(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    cid = _add_candidate(tmp_db_path, url="https://example.com/cached-article")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        assert candidate is not None

    # Pre-populate cache
    cached = _cache_path(tmp_path, "https://example.com/cached-article")
    cached.write_text(SAMPLE_ARTICLE_HTML, encoding="utf-8")

    # All network calls fail — only the pre-populated cache should be used
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(404, text="")
    sem = asyncio.Semaphore(1)
    source_map: dict[str, SourceEntry] = {}

    status = await _process_candidate(candidate, source_map, client, sem, tmp_path)

    assert status == "fetched"
    # Main article came from cache — no GET for that URL
    for call in client.get.call_args_list:
        assert "cached-article" not in str(call)


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_fetch_all_processes_new_candidates(
    tmp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_candidate(tmp_db_path, url="https://example.com/article-fetch-all")

    # Pre-populate cache so no real HTTP is needed
    cached = _cache_path(tmp_path, "https://example.com/article-fetch-all")
    cached.write_text(SAMPLE_ARTICLE_HTML, encoding="utf-8")

    from newsfeed.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.paths, "cache", tmp_path)

    registry = SourceRegistry(sources=[_SOURCE])
    counts = await fetch_all(registry)

    assert counts["fetched"] >= 1
    assert counts["total"] >= 1


@pytest.mark.unit
async def test_fetch_all_returns_zeros_when_no_new_candidates(
    tmp_db_path: Path,
) -> None:
    registry = SourceRegistry(sources=[_SOURCE])
    counts = await fetch_all(registry)

    assert counts == {"fetched": 0, "filtered": 0, "total": 0}


@pytest.mark.unit
def test_run_raises_on_missing_registry(tmp_db_path: Path, tmp_path: Path) -> None:
    from newsfeed.stage4_fetch import run

    with pytest.raises(FileNotFoundError):
        run(registry_path=tmp_path / "nope.json")
