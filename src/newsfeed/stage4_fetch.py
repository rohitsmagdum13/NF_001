"""Stage 4 — Fetch full article content and build context bundles.

Inputs
------
    SQLite: candidates table    (status='new')

Outputs
-------
    SQLite: candidates table    (status updated: 'new' → 'fetched' or 'filtered')
    SQLite: context_bundles     (main_text, sublinks_json, metadata_json per candidate)
    ./data/cache/               (raw HTML/PDF cached by content SHA-256)

What this stage does
--------------------
    For each ``status='new'`` candidate:

    1. Skip if already cached (check ``data/cache/`` by URL hash).
    2. Fetch the article URL with ``httpx`` (or local file read for html/md).
       Fall back to Playwright if ``requires_js=True`` on the source entry.
    3. Extract clean prose with ``trafilatura``.
    4. Parse sublinks matching configurable patterns (PDFs, ministerial pages).
    5. Fetch sublinks at depth=1; cache each.
    6. Build a ``ContextBundle`` JSON: main_text + sublinks + metadata.
    7. Persist bundle to ``context_bundles`` table and update candidate status.

Failure handling
----------------
    * HTTP errors / timeouts → candidate.status = 'filtered', logged at WARNING.
    * Trafilatura extracts nothing → keep raw HTML as main_text, log at WARNING.
    * Any unhandled exception → candidate.status = 'filtered', logged at ERROR.

Run directly
------------
    uv run python -m newsfeed.stage4_fetch
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from loguru import logger
from selectolax.parser import HTMLParser
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, ContextBundle, get_session
from newsfeed.schemas import SourceEntry, SourceRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "NewsfeedBot/1.0 (regulatory-newsfeed-poc)"
_DEFAULT_SUBLINK_PATTERNS = (r"\.pdf$", r"/ministers?/", r"/media-releases?/")
_MAX_SUBLINKS = 5  # per article, configurable in future

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, url: str, ext: str = "html") -> Path:
    return cache_dir / f"{_url_hash(url)}.{ext}"


def _extract_text(html: str) -> str:
    """Use trafilatura for main-content extraction; fall back to raw text."""
    result: str | None = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )
    if result:
        return result
    # Fallback: strip tags with selectolax
    tree = HTMLParser(html)
    return tree.text(separator="\n", strip=True)


def _find_sublinks(html: str, base_url: str, patterns: list[str]) -> list[str]:
    """Return internal links matching any of ``patterns`` (regex)."""
    tree = HTMLParser(html)
    base_netloc = urlparse(base_url).netloc
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    seen: set[str] = set()
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = (cast(dict[str, str], node.attrs).get("href") or "").strip()
        if not href:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        if urlparse(url).netloc != base_netloc and not href.endswith(".pdf"):
            continue
        if any(rx.search(url) for rx in compiled):
            links.append(url)
    return links[:_MAX_SUBLINKS]


# ---------------------------------------------------------------------------
# Async fetchers
# ---------------------------------------------------------------------------


async def _fetch_url(client: httpx.AsyncClient, url: str) -> str | None:
    """Fetch URL and return text content, or None on failure."""
    try:
        resp = await client.get(url, follow_redirects=True)
        if not resp.is_success:
            logger.debug("non-success HTTP status", url=url, status=resp.status_code)
            return None
        return resp.text
    except Exception as exc:
        logger.debug("HTTP fetch error", url=url, error=str(exc))
        return None


async def _fetch_with_playwright(url: str) -> str | None:
    """Playwright fallback for JS-rendered pages."""
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning("playwright not installed", url=url)
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=_USER_AGENT)
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            return await page.content()
        finally:
            await browser.close()


def _read_local_file(path: str) -> str | None:
    """Read a local HTML or MD file."""
    p = Path(path)
    if not p.exists():
        logger.warning("local file not found", path=path)
        return None
    return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Per-candidate processing helpers
# ---------------------------------------------------------------------------


async def _acquire_html(
    candidate: Candidate,
    source: SourceEntry | None,
    client: httpx.AsyncClient,
    cache_dir: Path,
) -> str | None:
    """Return raw HTML for a candidate — from cache, local file, or HTTP."""
    if candidate.source_type in ("html", "md") and source and source.local_path:
        return _read_local_file(source.local_path)

    if not candidate.url:
        return None

    cached = _cache_path(cache_dir, candidate.url)
    if cached.exists():
        logger.debug("cache hit", url=candidate.url)
        return cached.read_text(encoding="utf-8", errors="replace")

    uses_js = source.requires_js if source else False
    if uses_js:
        html = await _fetch_with_playwright(candidate.url)
    else:
        html = await _fetch_url(client, candidate.url)
    if html:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(html, encoding="utf-8")
    return html


async def _collect_sublinks(
    html: str,
    base_url: str,
    patterns: list[str],
    client: httpx.AsyncClient,
    cache_dir: Path,
) -> list[dict[str, str]]:
    """Fetch and extract text from sublinks found in html (depth=1)."""
    sublinks: list[dict[str, str]] = []
    for sub_url in _find_sublinks(html, base_url, patterns):
        sub_cached = _cache_path(cache_dir, sub_url, ext="html")
        if sub_cached.exists():
            sub_html: str | None = sub_cached.read_text(encoding="utf-8", errors="replace")
        else:
            sub_html = await _fetch_url(client, sub_url)
            if sub_html:
                sub_cached.parent.mkdir(parents=True, exist_ok=True)
                sub_cached.write_text(sub_html, encoding="utf-8")
        if sub_html:
            sublinks.append({"url": sub_url, "text": _extract_text(sub_html)})
    return sublinks


def _save_bundle(
    candidate_id: int,
    main_text: str,
    sublinks: list[dict[str, str]],
    metadata: dict[str, Any],
) -> None:
    """Upsert a ContextBundle and mark the candidate as 'fetched'."""
    with get_session() as session:
        existing = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == candidate_id)
        ).scalar_one_or_none()

        sl_json = json.dumps(sublinks, ensure_ascii=False)
        meta_json = json.dumps(metadata, ensure_ascii=False)

        if existing:
            existing.main_text = main_text
            existing.sublinks_json = sl_json
            existing.metadata_json = meta_json
        else:
            session.add(
                ContextBundle(
                    candidate_id=candidate_id,
                    main_text=main_text,
                    sublinks_json=sl_json,
                    metadata_json=meta_json,
                )
            )

        cand = session.get(Candidate, candidate_id)
        if cand:
            cand.status = "fetched"


async def _process_candidate(
    candidate: Candidate,
    source_map: dict[str, SourceEntry],
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cache_dir: Path,
) -> str:
    """Fetch + bundle one candidate. Returns new status string."""
    source = source_map.get(candidate.source_id)
    sublink_patterns = list(source.sublink_patterns) if source else []
    if not sublink_patterns:
        sublink_patterns = list(_DEFAULT_SUBLINK_PATTERNS)

    async with sem:
        html = await _acquire_html(candidate, source, client, cache_dir)
        if not html:
            logger.warning("no content fetched", candidate_id=candidate.id, url=candidate.url)
            return "filtered"

        main_text = _extract_text(html)
        sublinks = (
            await _collect_sublinks(html, candidate.url, sublink_patterns, client, cache_dir)
            if candidate.url
            else []
        )
        metadata: dict[str, Any] = {
            "fetched_at": _utcnow().isoformat(),
            "source_url": candidate.url,
            "source_type": candidate.source_type,
            "section_hint": candidate.section_hint,
            "jurisdiction_hint": candidate.jurisdiction_hint,
        }
        _save_bundle(candidate.id, main_text, sublinks, metadata)

    return "fetched"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_all(
    registry: SourceRegistry,
    *,
    batch_size: int = 50,
) -> dict[str, int]:
    """Fetch all ``status='new'`` candidates. Returns counts dict."""
    settings = get_settings()
    sem = asyncio.Semaphore(settings.pipeline.fetch_concurrency)
    cache_dir = settings.paths.cache
    cache_dir.mkdir(parents=True, exist_ok=True)

    source_map = {s.source_id: s for s in registry.sources}

    with get_session() as session:
        candidates: list[Candidate] = list(
            session.execute(select(Candidate).where(Candidate.status == "new")).scalars()
        )

    if not candidates:
        logger.info("fetch: no new candidates")
        return {"fetched": 0, "filtered": 0, "total": 0}

    logger.info("fetch start", candidates=len(candidates))

    headers = {"User-Agent": _USER_AGENT}
    timeout = httpx.Timeout(settings.pipeline.http_timeout_seconds)
    fetched_count = 0
    filtered_count = 0

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        # Process in batches to bound memory usage
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i : i + batch_size]
            tasks = [_process_candidate(c, source_map, client, sem, cache_dir) for c in batch]
            statuses = await asyncio.gather(*tasks, return_exceptions=True)

            for candidate, status in zip(batch, statuses, strict=True):
                if isinstance(status, Exception):
                    logger.error(
                        "fetch error",
                        candidate_id=candidate.id,
                        url=candidate.url,
                        error=str(status),
                    )
                    with get_session() as session:
                        cand = session.get(Candidate, candidate.id)
                        if cand:
                            cand.status = "filtered"
                    filtered_count += 1
                elif status == "fetched":
                    fetched_count += 1
                else:
                    filtered_count += 1

    counts = {"fetched": fetched_count, "filtered": filtered_count, "total": len(candidates)}
    logger.info("fetch complete", **counts)
    return counts


def run(registry_path: Path | None = None) -> dict[str, int]:
    """Entry point: load registry, fetch all new candidates, return counts."""
    settings = get_settings()
    registry_path = registry_path or settings.paths.source_registry

    raw = registry_path.read_text(encoding="utf-8").strip() if registry_path.exists() else ""
    if not raw:
        logger.warning("source registry missing or empty — skipping fetch", path=str(registry_path))
        return {"fetched": 0, "filtered": 0, "total": 0}

    data = json.loads(raw)
    registry = SourceRegistry.model_validate(data)
    return asyncio.run(fetch_all(registry))


if __name__ == "__main__":
    run()
