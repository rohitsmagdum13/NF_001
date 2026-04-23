"""Unit tests for Stage 1 — discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import feedparser
import httpx
import pytest

from newsfeed.schemas import SourceEntry
from newsfeed.stage1_discovery import (
    _discover_local_html,
    _discover_local_md,
    _DiscoveredItem,
    _items_from_feed,
    _items_from_html,
    _items_from_sitemap,
    _persist_batch,
    _try_html_listing,
    _try_rss,
    _try_sitemap,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
SAMPLE_RSS = (FIXTURES / "sample_rss.xml").read_text(encoding="utf-8")
SAMPLE_SITEMAP = (FIXTURES / "sample_sitemap.xml").read_text(encoding="utf-8")
SAMPLE_HTML = (FIXTURES / "sample_listing.html").read_text(encoding="utf-8")

_CUTOFF_2024 = datetime(2024, 1, 1, tzinfo=UTC)


def _make_source(
    source_type: str = "url",
    url: str = "https://example.com/news",
    local_path: str | None = None,
) -> SourceEntry:
    return SourceEntry(
        source_id="water_reform__national__abc123def4",
        source_type=source_type,  # type: ignore[arg-type]
        url=url if source_type == "url" else None,
        local_path=local_path,
        section="Water Reform",
        jurisdiction="National",
        source_label="National",
    )


# ---------------------------------------------------------------------------
# _items_from_feed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_items_from_feed_parses_rss() -> None:
    feed = feedparser.parse(SAMPLE_RSS)
    items = _items_from_feed(feed, _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/news/water-policy-2024" in urls
    assert "https://example.com/news/water-reform-update" in urls


@pytest.mark.unit
def test_items_from_feed_filters_old_entries() -> None:
    feed = feedparser.parse(SAMPLE_RSS)
    items = _items_from_feed(feed, _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/news/old-article" not in urls


@pytest.mark.unit
def test_items_from_feed_keeps_no_date_entries() -> None:
    """Items without a date must be kept (date_missing=True) not silently dropped."""
    feed = feedparser.parse(SAMPLE_RSS)
    items = _items_from_feed(feed, _make_source(), _CUTOFF_2024)

    no_date = [i for i in items if i.date_missing]
    assert no_date, "expected at least one date-missing item from sample RSS"
    assert all(i.pub_date is None for i in no_date)


@pytest.mark.unit
def test_items_from_feed_source_hints() -> None:
    source = _make_source()
    feed = feedparser.parse(SAMPLE_RSS)
    items = _items_from_feed(feed, source, _CUTOFF_2024)

    assert all(i.source.section == "Water Reform" for i in items)
    assert all(i.source.jurisdiction == "National" for i in items)


# ---------------------------------------------------------------------------
# _items_from_sitemap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_items_from_sitemap_parses() -> None:
    items = _items_from_sitemap(SAMPLE_SITEMAP, _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/article-recent-1" in urls
    assert "https://example.com/article-recent-2" in urls


@pytest.mark.unit
def test_items_from_sitemap_filters_old() -> None:
    items = _items_from_sitemap(SAMPLE_SITEMAP, _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/article-too-old" not in urls


@pytest.mark.unit
def test_items_from_sitemap_keeps_no_date() -> None:
    items = _items_from_sitemap(SAMPLE_SITEMAP, _make_source(), _CUTOFF_2024)

    no_date = [i for i in items if i.date_missing]
    assert any(i.url == "https://example.com/article-no-date" for i in no_date)


@pytest.mark.unit
def test_items_from_sitemap_rejects_malformed_xml() -> None:
    items = _items_from_sitemap("this is not xml", _make_source(), _CUTOFF_2024)
    assert items == []


# ---------------------------------------------------------------------------
# _items_from_html
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_items_from_html_extracts_article_links() -> None:
    items = _items_from_html(SAMPLE_HTML, "https://example.com", _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/news/water-infrastructure-announcement" in urls
    assert "https://example.com/news/water-policy-review" in urls


@pytest.mark.unit
def test_items_from_html_filters_old_dated_items() -> None:
    items = _items_from_html(SAMPLE_HTML, "https://example.com", _make_source(), _CUTOFF_2024)

    urls = [i.url for i in items]
    assert "https://example.com/news/old-news-story" not in urls


@pytest.mark.unit
def test_items_from_html_filters_external_links() -> None:
    html = """<html><body>
        <h2><a href="https://other.com/article">External Article Title Here</a></h2>
        <h2><a href="/local-article">A Local Article With Long Title</a></h2>
    </body></html>"""
    items = _items_from_html(html, "https://example.com", _make_source(), _CUTOFF_2024)
    urls = [i.url for i in items]
    assert not any("other.com" in u for u in urls)
    assert "https://example.com/local-article" in urls


@pytest.mark.unit
def test_items_from_html_skips_short_nav_titles() -> None:
    html = """<html><body>
        <nav>
            <a href="/about">About</a>
            <a href="/news/proper-article-headline-text">Proper Article Headline Text</a>
        </nav>
    </body></html>"""
    items = _items_from_html(html, "https://example.com", _make_source(), _CUTOFF_2024)
    urls = [i.url for i in items]
    assert not any("/about" in u for u in urls)
    assert "https://example.com/news/proper-article-headline-text" in urls


# ---------------------------------------------------------------------------
# Async fetchers (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_try_rss_returns_items_for_valid_feed() -> None:
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, text=SAMPLE_RSS)

    items = await _try_rss(client, source, _CUTOFF_2024)

    assert len(items) >= 2
    assert all(i.source.source_id == source.source_id for i in items)


@pytest.mark.unit
async def test_try_rss_falls_back_to_probe_when_direct_is_html() -> None:
    """Direct URL is an HTML page; probe path finds the RSS feed."""
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)

    html_resp = httpx.Response(200, text="<html><body>Not a feed</body></html>")
    rss_resp = httpx.Response(200, text=SAMPLE_RSS)

    # First call (direct URL) → HTML; subsequent calls → RSS on some probe
    client.get.side_effect = [html_resp] + [rss_resp] * 10

    items = await _try_rss(client, source, _CUTOFF_2024)
    assert len(items) >= 1


@pytest.mark.unit
async def test_try_rss_returns_empty_for_non_feed_page() -> None:
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, text="<html><body>Not a feed</body></html>")

    items = await _try_rss(client, source, _CUTOFF_2024)
    assert items == []


@pytest.mark.unit
async def test_try_sitemap_returns_items() -> None:
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, text=SAMPLE_SITEMAP)

    items = await _try_sitemap(client, source, _CUTOFF_2024)
    assert len(items) >= 2


@pytest.mark.unit
async def test_try_sitemap_returns_empty_on_404() -> None:
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(404, text="Not found")

    items = await _try_sitemap(client, source, _CUTOFF_2024)
    assert items == []


@pytest.mark.unit
async def test_try_html_listing_returns_items() -> None:
    source = _make_source()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, text=SAMPLE_HTML)

    items = await _try_html_listing(client, source, _CUTOFF_2024)
    assert len(items) >= 1


# ---------------------------------------------------------------------------
# _persist_batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_persist_batch_inserts_new_candidates(tmp_db_path: Path) -> None:
    source = _make_source()
    item = _DiscoveredItem(
        url="https://example.com/article-persist-test",
        title="A Brand New Article About Water Reform",
        pub_date=datetime(2024, 4, 1, tzinfo=UTC),
        date_missing=False,
        source=source,
    )
    counts = _persist_batch([item])
    assert counts["new"] == 1
    assert counts["skipped"] == 0


@pytest.mark.unit
def test_persist_batch_skips_seen_hash(tmp_db_path: Path) -> None:
    source = _make_source()
    item = _DiscoveredItem(
        url="https://example.com/article-dedup-test",
        title="Article That Will Be Seen Twice Today",
        pub_date=datetime(2024, 4, 1, tzinfo=UTC),
        date_missing=False,
        source=source,
    )
    _persist_batch([item])
    counts = _persist_batch([item])  # second call — same hash
    assert counts["new"] == 0
    assert counts["skipped"] == 1


@pytest.mark.unit
def test_persist_batch_handles_empty_list(tmp_db_path: Path) -> None:
    counts = _persist_batch([])
    assert counts == {"new": 0, "skipped": 0}


@pytest.mark.unit
def test_persist_batch_date_missing_item_gets_status_new(tmp_db_path: Path) -> None:
    """Items without a date should still be persisted as status='new'."""
    from sqlalchemy import select as sa_select

    from newsfeed.db import Candidate, get_session

    source = _make_source()
    item = _DiscoveredItem(
        url="https://example.com/article-no-date-persist",
        title="Article With No Publication Date At All",
        pub_date=None,
        date_missing=True,
        source=source,
    )
    _persist_batch([item])

    with get_session() as session:
        candidate = session.execute(
            sa_select(Candidate).where(
                Candidate.url == "https://example.com/article-no-date-persist"
            )
        ).scalar_one_or_none()

    assert candidate is not None
    assert candidate.status == "new"
    assert candidate.pub_date is None


# ---------------------------------------------------------------------------
# Local source discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_local_html_creates_candidate(tmp_path: Path) -> None:
    html_file = tmp_path / "article.html"
    html_file.write_text(
        "<html><body><h1>Water Policy Update</h1>"
        "<time datetime='2024-04-01'>1 April 2024</time>"
        "<p>Content here.</p></body></html>",
        encoding="utf-8",
    )
    source = _make_source(source_type="html", url=None, local_path=str(html_file))

    items = _discover_local_html(source, _CUTOFF_2024)

    assert len(items) == 1
    assert items[0].title == "Water Policy Update"
    assert items[0].pub_date is not None


@pytest.mark.unit
def test_discover_local_html_missing_file_returns_empty(tmp_path: Path) -> None:
    source = _make_source(source_type="html", url=None, local_path=str(tmp_path / "nope.html"))
    items = _discover_local_html(source, _CUTOFF_2024)
    assert items == []


@pytest.mark.unit
def test_discover_local_md_creates_candidate(tmp_path: Path) -> None:
    md_file = tmp_path / "article.md"
    md_file.write_text(
        "# Water Reform Update March 2024\n\nDetails here.\n",
        encoding="utf-8",
    )
    source = _make_source(source_type="md", url=None, local_path=str(md_file))

    items = _discover_local_md(source, _CUTOFF_2024)

    assert len(items) == 1
    assert "Water Reform Update" in (items[0].title or "")
