"""Stage 1 — Discovery: find candidate articles from the source registry.

Inputs
------
    data/source_registry.json   (SourceRegistry from Stage 0)

Outputs
-------
    SQLite: candidates table    (new rows with status='new')
    SQLite: seen_hashes table   (dedup ledger updated)

Discovery chain (URL sources)
-----------------------------
    1. Fetch source URL; if feedparser finds entries → RSS path done.
    2. Probe common RSS endpoint suffixes (``/feed``, ``/rss.xml``, …).
    3. Try ``{domain}/sitemap.xml`` with lxml.
    4. Parse listing HTML with selectolax.
    5. If ``requires_js=True`` on the SourceEntry, use Playwright for step 4.

Date handling
-------------
    Items with a parseable date older than ``lookback_days`` are dropped.
    Items with NO parseable date are kept (``pub_date=NULL``) and a WARNING
    is logged — never silently dropped per spec.

Run directly
------------
    uv run python -m newsfeed.stage1_discovery
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import dateparser
import feedparser
import httpx
from loguru import logger
from lxml import etree
from selectolax.parser import HTMLParser
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, SeenHash, get_session
from newsfeed.schemas import SourceEntry, SourceRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "NewsfeedBot/1.0 (regulatory-newsfeed-poc)"
_URL_LINE_RE = re.compile(r"https?://[^\s<>)\]\"']+")
_RSS_PROBE_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/news/feed")
_MIN_TITLE_LEN = 10  # characters — filters out navigation links
_MAX_HTML_LINKS = 200  # cap per listing page
_DATE_SETTINGS: dict[str, Any] = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DAY_OF_MONTH": "first",
    "STRICT_PARSING": False,
}
_DATE_PATTERN = re.compile(
    r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)
# Matches a 4-digit year inside a URL path segment, e.g. /news/2025/article-slug
_URL_YEAR_RE = re.compile(r"/(\d{4})/")
# Optional year+month, e.g. /news/2025/09/article
_URL_YEAR_MONTH_RE = re.compile(r"/(\d{4})/(\d{2})/")

# Selectors for article links in HTML listings (tried in order, first match used).
# Deliberately excludes "li a[href]" — too broad, picks up all nav menu items.
_ARTICLE_LINK_SELECTORS = [
    "article a[href]",
    "h1 a[href]",
    "h2 a[href]",
    "h3 a[href]",
    ".news-item a[href]",
    ".post-title a[href]",
    ".media-release a[href]",
    ".listing-item a[href]",
    ".entry-title a[href]",
]


# ---------------------------------------------------------------------------
# Internal DTO
# ---------------------------------------------------------------------------


@dataclass
class _DiscoveredItem:
    """One candidate before DB write."""

    url: str
    title: str | None
    pub_date: datetime | None
    date_missing: bool
    source: SourceEntry

    @property
    def content_hash(self) -> str:
        key = self.url + "\x00" + (self.title or "").lower().strip()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _struct_time_to_dt(st: Any) -> datetime | None:
    if st is None:
        return None
    try:
        return datetime(st[0], st[1], st[2], st[3], st[4], st[5], tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        return None


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    result: datetime | None = dateparser.parse(text, settings=_DATE_SETTINGS)
    return result


def _date_from_url(url: str) -> datetime | None:
    """Extract an approximate date from a URL path segment.

    Handles three patterns (most precise first):
      /2025/09/15/  → 2025-09-15
      /2025/09/     → 2025-09-01
      /2025/        → 2025-12-31  (use end-of-year so cutoff comparison is safe)

    Returns None when no year-shaped segment is found, or the year is the
    current calendar year (too coarse to tell if within lookback window).
    """
    current_year = _utcnow().year

    m = _URL_YEAR_MONTH_RE.search(url)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if year != current_year and 2000 <= year <= current_year:
            try:
                return datetime(year, month, 1, tzinfo=UTC)
            except ValueError:
                pass

    m2 = _URL_YEAR_RE.search(url)
    if m2:
        year = int(m2.group(1))
        if year != current_year and 2000 <= year <= current_year:
            # Use Dec 31 — guarantees the date is before any same-year cutoff
            return datetime(year, 12, 31, tzinfo=UTC)

    return None


# ---------------------------------------------------------------------------
# Pure feed parsers (no network — easy to unit test)
# ---------------------------------------------------------------------------


def _items_from_feed(feed: Any, source: SourceEntry, cutoff: datetime) -> list[_DiscoveredItem]:
    """Convert feedparser result to _DiscoveredItem list, applying cutoff."""
    items: list[_DiscoveredItem] = []
    for entry in feed.entries:
        url: str | None = getattr(entry, "link", None) or getattr(entry, "id", None)
        if not url:
            continue
        title: str | None = getattr(entry, "title", None)
        pub_date = _struct_time_to_dt(
            getattr(entry, "published_parsed", None)
        ) or _struct_time_to_dt(getattr(entry, "updated_parsed", None))
        date_missing = pub_date is None
        if pub_date and pub_date < cutoff:
            continue
        items.append(
            _DiscoveredItem(
                url=url,
                title=title,
                pub_date=pub_date,
                date_missing=date_missing,
                source=source,
            )
        )
    return items


def _items_from_sitemap(
    xml_text: str, source: SourceEntry, cutoff: datetime
) -> list[_DiscoveredItem]:
    """Parse sitemap.xml text, apply cutoff, return items."""
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    items: list[_DiscoveredItem] = []
    for url_el in root.findall(".//sm:url", ns):
        loc = url_el.findtext("sm:loc", namespaces=ns)
        lastmod = url_el.findtext("sm:lastmod", namespaces=ns)
        if not loc:
            continue
        pub_date = _parse_date(lastmod.strip()) if lastmod else None
        date_missing = pub_date is None
        if pub_date and pub_date < cutoff:
            continue
        items.append(
            _DiscoveredItem(
                url=loc.strip(),
                title=None,
                pub_date=pub_date,
                date_missing=date_missing,
                source=source,
            )
        )
    return items


def _extract_date_near_node(node: Any) -> datetime | None:
    """Walk up to 5 ancestor elements looking for a <time datetime> or date text."""
    current = node.parent
    for _ in range(5):
        if current is None:
            break
        try:
            for time_node in current.css("time"):
                dt_str = cast(dict[str, str], time_node.attrs or {}).get("datetime") or ""
                if dt_str:
                    parsed = _parse_date(dt_str)
                    if parsed:
                        return parsed
            text = current.text(strip=True)[:300]
            m = _DATE_PATTERN.search(text)
            if m:
                parsed = _parse_date(m.group(0))
                if parsed:
                    return parsed
        except Exception:
            pass
        current = getattr(current, "parent", None)
    return None


# Nav/chrome containers whose links are always menus, never content.
_NAV_SELECTORS = ("nav", "header", "footer", "aside", "[role='navigation']", "[role='banner']")

# Candidate content-area selectors (tried in order; first that yields links wins).
_CONTENT_AREA_SELECTORS = (
    "main",
    "[role='main']",
    "#content",
    "#main",
    ".content",
    ".main-content",
    ".page-content",
    ".post-content",
    ".entry-content",
    ".site-content",
)


def _nav_urls(tree: HTMLParser, base_url: str) -> set[str]:
    """Collect all URLs that appear inside nav/header/footer/aside elements."""
    urls: set[str] = set()
    for sel in _NAV_SELECTORS:
        for node in tree.css(f"{sel} a[href]"):
            href = (cast(dict[str, str], node.attrs).get("href") or "").strip()
            if href:
                urls.add(urljoin(base_url, href))
    return urls


def _items_from_html(
    html_text: str,
    base_url: str,
    source: SourceEntry,
    cutoff: datetime,
) -> list[_DiscoveredItem]:
    """Extract candidate article links from an HTML listing page.

    Strategy (dynamic — works regardless of URL path structure):
      1. Collect all URLs that live inside nav/header/footer/aside → mark as
         navigation and exclude them from every subsequent pass.
      2. Try known content-area selectors (main, #content, .content, …) to
         find links in the page body only.
      3. If no content-area selector matches, fall back to semantic article
         selectors (article, h2 a, .news-item, …).
      4. Last resort: all same-domain links minus the nav URLs.
    """
    tree = HTMLParser(html_text)
    base_netloc = urlparse(base_url).netloc
    nav_urls = _nav_urls(tree, base_url)
    seen: set[str] = set()

    def _collect(nodes: list[Any]) -> list[_DiscoveredItem]:
        result: list[_DiscoveredItem] = []
        for node in nodes[:_MAX_HTML_LINKS]:
            href = (cast(dict[str, str], node.attrs).get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            url = urljoin(base_url, href)
            if urlparse(url).netloc != base_netloc:
                continue
            if url in nav_urls:          # skip navigation links
                continue
            if url in seen:
                continue
            seen.add(url)
            title = node.text(strip=True) or None
            if not title or len(title) < _MIN_TITLE_LEN:
                continue
            pub_date = _extract_date_near_node(node) or _date_from_url(url)
            date_missing = pub_date is None
            if pub_date and pub_date < cutoff:
                continue
            result.append(
                _DiscoveredItem(
                    url=url,
                    title=title,
                    pub_date=pub_date,
                    date_missing=date_missing,
                    source=source,
                )
            )
        return result

    # Pass 1 — content-area containers (most precise, nav already excluded above)
    for sel in _CONTENT_AREA_SELECTORS:
        area_nodes = list(tree.css(f"{sel} a[href]"))
        if not area_nodes:
            continue
        items = _collect(area_nodes)
        if items:
            logger.debug(
                "html listing: content-area selector matched",
                base_url=base_url,
                selector=sel,
                kept=len(items),
            )
            return items

    # Pass 2 — semantic article/heading selectors
    semantic_nodes: list[Any] = []
    for sel in _ARTICLE_LINK_SELECTORS:
        semantic_nodes.extend(tree.css(sel))
    items = _collect(semantic_nodes)
    if items:
        logger.debug(
            "html listing: semantic selectors matched",
            base_url=base_url,
            kept=len(items),
        )
        return items

    # Pass 3 — all same-domain links minus nav (broadest fallback)
    logger.debug(
        "html listing: fallback to all same-domain non-nav links",
        base_url=base_url,
    )
    return _collect(list(tree.css("a[href]")))


# ---------------------------------------------------------------------------
# Local-file discovery
# ---------------------------------------------------------------------------

_FILENAME_HINT_RE = re.compile(r"^([^_].+?)__([^_].+?)__(.+)$")


def _hints_from_filename(
    stem: str,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> tuple[str | None, str | None]:
    """Try to extract section and jurisdiction from a filename stem.

    Expects the pattern: <Section>__<Jurisdiction>__<title>
    Underscores within a part are replaced with spaces for matching.
    Returns (section, jurisdiction) or (None, None) if not matched.
    """
    m = _FILENAME_HINT_RE.match(stem)
    if not m:
        return None, None
    raw_section = m.group(1).replace("_", " ").strip()
    raw_juris = m.group(2).replace("_", " ").strip()
    section = next((s for s in valid_sections if s.lower() == raw_section.lower()), None)
    juris = next((j for j in valid_jurisdictions if j.lower() == raw_juris.lower()), None)
    return section, juris


def _urls_from_txt(path: Path) -> list[str]:
    """Read a .txt file and return every line that looks like an http(s) URL.

    Lines starting with # are treated as comments and ignored.
    """
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls_in_line = _URL_LINE_RE.findall(line)
        for u in urls_in_line:
            cleaned = u.rstrip(".,;:)")
            if cleaned:
                urls.append(cleaned)
    return urls


async def _discover_txt_file(
    path: Path,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cutoff: datetime,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> list[_DiscoveredItem]:
    """Read URLs from a .txt file and run the full discovery chain on each."""
    urls = _urls_from_txt(path)
    if not urls:
        logger.warning("local_articles: .txt file has no URLs", file=path.name)
        return []

    section_hint, juris_hint = _hints_from_filename(path.stem, valid_sections, valid_jurisdictions)

    items: list[_DiscoveredItem] = []
    for url in urls:
        source = SourceEntry(
            source_id=f"txt__{path.stem[:30]}__{hashlib.sha256(url.encode()).hexdigest()[:8]}",
            source_type="url",
            url=url,
            section=section_hint or valid_sections[0],
            jurisdiction=juris_hint or valid_jurisdictions[0],
            source_label=path.stem,
        )
        logger.info("local_articles: discovering URL from .txt", file=path.name, url=url)
        discovered = await _discover_url(source, client, sem, cutoff)
        items.extend(discovered)
        logger.info(
            "local_articles: .txt URL done",
            url=url,
            candidates=len(discovered),
        )

    return items


async def _scan_local_articles_folder_async(
    folder: Path,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cutoff: datetime,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> list[_DiscoveredItem]:
    """Scan local_articles/ for .html/.md files.

    .txt URL files are intentionally skipped here — Stage 0 reads them and
    writes their URLs into source_registry.json, so Stage 1 processes those
    URLs through the normal registry URL-discovery path.
    """
    items: list[_DiscoveredItem] = []
    if not folder.exists():
        return items

    for path in sorted(folder.iterdir()):
        suffix = path.suffix.lower()

        if suffix in (".html", ".htm", ".md"):
            section_hint, juris_hint = _hints_from_filename(
                path.stem, valid_sections, valid_jurisdictions
            )
            source = SourceEntry(
                source_id=f"local__{path.stem[:40]}",
                source_type="html" if suffix in (".html", ".htm") else "md",
                local_path=str(path),
                section=section_hint or valid_sections[0],
                jurisdiction=juris_hint or valid_jurisdictions[0],
                source_label="Local",
            )
            discovered = (
                _discover_local_html(source, cutoff)
                if suffix in (".html", ".htm")
                else _discover_local_md(source, cutoff)
            )
            if discovered:
                items.extend(discovered)
                logger.info(
                    "local_articles: found file",
                    file=path.name,
                    section_hint=section_hint,
                    jurisdiction_hint=juris_hint,
                )

    return items


def _scan_local_articles_folder(
    folder: Path,
    cutoff: datetime,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> list[_DiscoveredItem]:
    """Sync wrapper kept for unit-test compatibility (.html/.md only)."""
    items: list[_DiscoveredItem] = []
    if not folder.exists():
        return items

    for path in sorted(folder.iterdir()):
        suffix = path.suffix.lower()
        if suffix not in (".html", ".htm", ".md"):
            continue

        section_hint, juris_hint = _hints_from_filename(
            path.stem, valid_sections, valid_jurisdictions
        )
        source = SourceEntry(
            source_id=f"local__{path.stem[:40]}",
            source_type="html" if suffix in (".html", ".htm") else "md",
            local_path=str(path),
            section=section_hint or valid_sections[0],
            jurisdiction=juris_hint or valid_jurisdictions[0],
            source_label="Local",
        )
        discovered = (
            _discover_local_html(source, cutoff)
            if suffix in (".html", ".htm")
            else _discover_local_md(source, cutoff)
        )
        if discovered:
            items.extend(discovered)
            logger.info(
                "local_articles: found file",
                file=path.name,
                section_hint=section_hint,
                jurisdiction_hint=juris_hint,
            )

    return items


def _discover_local_html(source: SourceEntry, cutoff: datetime) -> list[_DiscoveredItem]:
    """Local .html file treated as a single article source."""
    if not source.local_path:
        logger.warning("local_html source missing local_path", source_id=source.source_id)
        return []
    path = Path(source.local_path)
    if not path.exists():
        logger.warning("local file not found", path=str(path), source_id=source.source_id)
        return []
    html_text = path.read_text(encoding="utf-8", errors="replace")
    tree = HTMLParser(html_text)
    title: str | None = None
    for sel in ("h1", "h2", "title"):
        nodes = tree.css(sel)
        if nodes:
            t = nodes[0].text(strip=True)
            if t:
                title = t
                break
    pub_date: datetime | None = None
    for t_node in tree.css("time[datetime]"):
        dt_str = cast(dict[str, str], t_node.attrs or {}).get("datetime") or ""
        pub_date = _parse_date(dt_str)
        if pub_date:
            break
    if pub_date and pub_date < cutoff:
        logger.info("local HTML too old, skipping", path=str(path))
        return []
    return [
        _DiscoveredItem(
            url=source.local_path,
            title=title,
            pub_date=pub_date,
            date_missing=pub_date is None,
            source=source,
        )
    ]


def _discover_local_md(source: SourceEntry, cutoff: datetime) -> list[_DiscoveredItem]:
    """Local .md file treated as a single article source."""
    if not source.local_path:
        logger.warning("local_md source missing local_path", source_id=source.source_id)
        return []
    path = Path(source.local_path)
    if not path.exists():
        logger.warning("local file not found", path=str(path), source_id=source.source_id)
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    title: str | None = None
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            title = stripped
            break
    pub_date: datetime | None = None
    m = _DATE_PATTERN.search(text[:500])
    if m:
        pub_date = _parse_date(m.group(0))
    if pub_date and pub_date < cutoff:
        logger.info("local MD too old, skipping", path=str(path))
        return []
    return [
        _DiscoveredItem(
            url=source.local_path,
            title=title,
            pub_date=pub_date,
            date_missing=pub_date is None,
            source=source,
        )
    ]


# ---------------------------------------------------------------------------
# Async HTTP fetchers
# ---------------------------------------------------------------------------


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, follow_redirects=True)
        if not resp.is_success:
            return None
        return resp.text
    except Exception as exc:
        logger.debug("HTTP fetch failed", url=url, error=str(exc))
        return None


async def _try_rss(
    client: httpx.AsyncClient,
    source: SourceEntry,
    cutoff: datetime,
) -> list[_DiscoveredItem]:
    """Try source URL as RSS/Atom; if not a feed, probe common RSS paths."""
    assert source.url

    # 1. Direct URL
    text = await _fetch_text(client, source.url)
    if text:
        feed = feedparser.parse(text)
        if feed.entries:
            logger.debug("RSS via direct URL", url=source.url, entries=len(feed.entries))
            return _items_from_feed(feed, source, cutoff)

    # 2. Probe common RSS suffixes
    parsed = urlparse(source.url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    probes = [f"{base}{path}" for path in _RSS_PROBE_PATHS]
    probes.append(source.url.rstrip("/") + "/feed")

    for probe_url in probes:
        probe_text = await _fetch_text(client, probe_url)
        if probe_text:
            feed = feedparser.parse(probe_text)
            if feed.entries:
                logger.debug("RSS via probe", probe=probe_url, entries=len(feed.entries))
                return _items_from_feed(feed, source, cutoff)

    return []


async def _try_sitemap(
    client: httpx.AsyncClient,
    source: SourceEntry,
    cutoff: datetime,
) -> list[_DiscoveredItem]:
    """Try {domain}/sitemap.xml."""
    assert source.url
    parsed = urlparse(source.url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    text = await _fetch_text(client, sitemap_url)
    if not text:
        return []
    items = _items_from_sitemap(text, source, cutoff)
    if items:
        logger.debug("discovered via sitemap", url=sitemap_url, count=len(items))
    return items


def _find_content_nav_urls(
    html_text: str,
    base_url: str,
    patterns: list[str],
) -> list[str]:
    """Scan <nav> links and return those whose text matches content category patterns.

    For example, given patterns ["news", "publication", "event", "consultation"],
    a nav link labelled "Media Releases" won't match but "News Articles",
    "Publications", "Events", or "Consultation Hub" will.

    Returns a deduplicated list of same-domain URLs in document order.
    """
    if not patterns:
        return []
    compiled = [re.compile(re.escape(p), re.IGNORECASE) for p in patterns]
    tree = HTMLParser(html_text)
    base_netloc = urlparse(base_url).netloc
    found: list[str] = []
    seen: set[str] = set()

    for sel in _NAV_SELECTORS:
        for node in tree.css(f"{sel} a[href]"):
            href = (cast(dict[str, str], node.attrs).get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            url = urljoin(base_url, href)
            if urlparse(url).netloc != base_netloc:
                continue
            if url in seen:
                continue
            text = node.text(strip=True)
            if any(p.search(text) for p in compiled):
                seen.add(url)
                found.append(url)
                logger.debug(
                    "content nav link matched",
                    text=text,
                    url=url,
                )

    return found


async def _try_html_listing(
    client: httpx.AsyncClient,
    source: SourceEntry,
    cutoff: datetime,
) -> list[_DiscoveredItem]:
    """Parse source URL as a news listing page.

    If the page contains nav links matching ``content_nav_patterns`` (e.g.
    "News Articles", "Publications", "Events"), those nav URLs are used as
    listing pages instead of the source URL itself.  This lets you point the
    pipeline at a site homepage or section page and have it automatically
    find and crawl the right content sections.
    """
    assert source.url
    text = await _fetch_text(client, source.url)
    if not text:
        return []

    settings = get_settings()
    content_nav_urls = _find_content_nav_urls(
        text, source.url, settings.pipeline.content_nav_patterns
    )

    if content_nav_urls:
        logger.info(
            "content nav links found — crawling each as a listing page",
            base_url=source.url,
            listing_pages=content_nav_urls,
        )
        all_items: list[_DiscoveredItem] = []
        for listing_url in content_nav_urls:
            listing_text = await _fetch_text(client, listing_url)
            if not listing_text:
                continue
            items = _items_from_html(listing_text, listing_url, source, cutoff)
            if items:
                logger.debug(
                    "discovered via content nav listing",
                    url=listing_url,
                    count=len(items),
                )
                all_items.extend(items)
        return all_items

    # No content nav links found — treat the source URL itself as the listing page
    items = _items_from_html(text, source.url, source, cutoff)
    if items:
        logger.debug("discovered via HTML listing", url=source.url, count=len(items))
    return items


async def _try_playwright(source: SourceEntry, cutoff: datetime) -> list[_DiscoveredItem]:
    """Playwright fallback for JS-rendered listing pages."""
    assert source.url
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning("playwright not installed — skipping JS source", url=source.url)
        return []

    logger.info("using playwright for JS-rendered page", url=source.url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=_USER_AGENT)
            await page.goto(source.url, wait_until="networkidle", timeout=30_000)
            html = await page.content()
        finally:
            await browser.close()

    return _items_from_html(html, source.url, source, cutoff)


async def _discover_url(
    source: SourceEntry,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cutoff: datetime,
) -> list[_DiscoveredItem]:
    """Full discovery chain: RSS → sitemap → HTML listing (→ Playwright)."""
    assert source.url
    async with sem:
        items = await _try_rss(client, source, cutoff)
        if items:
            return items
        items = await _try_sitemap(client, source, cutoff)
        if items:
            return items
        if source.requires_js:
            return await _try_playwright(source, cutoff)
        return await _try_html_listing(client, source, cutoff)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_batch(items: list[_DiscoveredItem]) -> dict[str, int]:
    """Write new items to DB. Returns ``{"new": N, "skipped": M}``."""
    if not items:
        return {"new": 0, "skipped": 0}

    new_count = 0
    skipped_count = 0

    with get_session() as session:
        hashes = [item.content_hash for item in items]
        existing: set[str] = {
            row
            for (row,) in session.execute(select(SeenHash.hash).where(SeenHash.hash.in_(hashes)))
        }

        for item in items:
            h = item.content_hash
            if h in existing:
                skipped_count += 1
                continue

            if item.date_missing:
                logger.warning(
                    "candidate has no date — flagged for editor review",
                    url=item.url,
                    title=item.title,
                    source_id=item.source.source_id,
                )

            is_local = item.source.source_type in ("html", "md") and item.source.local_path
            candidate = Candidate(
                source_id=item.source.source_id,
                source_type=item.source.source_type,
                url=None if is_local else item.url,
                title=item.title,
                pub_date=item.pub_date,
                section_hint=item.source.section,
                jurisdiction_hint=item.source.jurisdiction,
                source_label=item.source.source_label,
                hash=h,
                status="new",
            )
            session.add(candidate)
            session.add(SeenHash(hash=h, source_id=item.source.source_id, url=item.url))
            existing.add(h)
            new_count += 1

    return {"new": new_count, "skipped": skipped_count}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def discover_all(
    registry: SourceRegistry,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """Discover candidates from all registry sources.

    Returns ``{"sources": N, "new": M, "skipped": K, "errors": E}``.
    """
    settings = get_settings()
    cutoff = _utcnow() - timedelta(days=settings.pipeline.lookback_days)
    sem = asyncio.Semaphore(settings.pipeline.fetch_concurrency)

    sources = list(registry.sources)
    if limit is not None:
        sources = sources[:limit]

    logger.info(
        "discovery start",
        sources=len(sources),
        lookback_days=settings.pipeline.lookback_days,
        cutoff=cutoff.date().isoformat(),
    )

    headers = {"User-Agent": _USER_AGENT}
    timeout = httpx.Timeout(settings.pipeline.http_timeout_seconds)

    all_items: list[_DiscoveredItem] = []
    error_count = 0

    url_sources = [s for s in sources if s.source_type == "url" and s.url]

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        tasks = [_discover_url(s, client, sem, cutoff) for s in url_sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # local_articles folder — .txt files need HTTP client (async)
        local_folder = settings.paths.local_articles
        try:
            local_async_items = await _scan_local_articles_folder_async(
                local_folder, client, sem, cutoff, settings.sections, settings.jurisdictions
            )
            if local_async_items:
                logger.info("local_articles: total discovered", count=len(local_async_items))
            all_items.extend(local_async_items)
        except Exception as exc:
            logger.error("local_articles folder error", error=str(exc))
            error_count += 1

    for source, result in zip(url_sources, results, strict=True):
        if isinstance(result, Exception):
            logger.error("discovery error", source_id=source.source_id, error=str(result))
            error_count += 1
        else:
            all_items.extend(result)  # type: ignore[arg-type]

    # Local sources from registry (sync — disk reads)
    for source in sources:
        if source.source_type == "html" and source.local_path:
            try:
                all_items.extend(_discover_local_html(source, cutoff))
            except Exception as exc:
                logger.error(
                    "local HTML discovery error", source_id=source.source_id, error=str(exc)
                )
                error_count += 1
        elif source.source_type == "md" and source.local_path:
            try:
                all_items.extend(_discover_local_md(source, cutoff))
            except Exception as exc:
                logger.error("local MD discovery error", source_id=source.source_id, error=str(exc))
                error_count += 1

    counts = _persist_batch(all_items)
    counts["sources"] = len(sources)
    counts["errors"] = error_count
    logger.info("discovery complete", **counts)
    return counts


def run(
    registry_path: Path | None = None,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """Entry point: load source registry → run discovery → return counts.

    Returns ``{"sources": N, "new": M, "skipped": K, "errors": E}``.
    """
    settings = get_settings()
    registry_path = registry_path or settings.paths.source_registry

    raw = registry_path.read_text(encoding="utf-8").strip() if registry_path.exists() else ""
    if not raw:
        logger.warning(
            "source registry missing or empty — skipping URL sources, will still scan local_articles/",
            path=str(registry_path),
        )
        registry = SourceRegistry()
    else:
        data = json.loads(raw)
        registry = SourceRegistry.model_validate(data)
        logger.info(
            "loaded source registry", sources=len(registry.sources), path=str(registry_path)
        )

    return asyncio.run(discover_all(registry, limit=limit))


if __name__ == "__main__":
    run()
