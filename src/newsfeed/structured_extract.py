"""Extract structured article metadata (JSON-LD + meta tags) from raw HTML.

Why this exists
---------------
    Trafilatura gives us clean prose, but throws away publisher-supplied
    structured data: schema.org Article fields, Open Graph metadata, and
    article-published-time meta tags. Government and news sites publish
    these consistently — they're the most reliable source of titles,
    publication dates, authors, and publishers.

    Stage 4 calls ``extract_structured(html)`` BEFORE trafilatura so that:
      • Candidates with no Stage-1 date can be backfilled from JSON-LD.
      • Context bundles carry rich metadata (author, publisher) for
        downstream attribution.
      • Stage 7 drafting has structured fields to slot, reducing
        hallucination risk.

Extraction order (highest fidelity first)
-----------------------------------------
    1. JSON-LD ``<script type="application/ld+json">`` documents typed
       as ``Article``, ``NewsArticle``, ``Report``, ``BlogPosting``, or
       contained inside an ``@graph`` array.
    2. Open Graph + Twitter card meta tags
       (``og:title``, ``article:published_time``, etc.).
    3. Standard ``<meta>`` tags (``description``, ``author``).
    4. ``<title>`` tag fallback for title.

All dates are parsed via ``dateparser`` and returned as timezone-aware
``datetime`` objects. Returns ``None`` if nothing useful is found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import dateparser
from loguru import logger
from selectolax.parser import HTMLParser

# Schema.org @types we treat as "this is an article"
_ARTICLE_TYPES = frozenset({
    "Article",
    "NewsArticle",
    "BlogPosting",
    "Report",
    "GovernmentService",
    "PublicationIssue",
    "WebPage",  # last-resort
})

# How dateparser should interpret strings (UTC-aware, lenient)
_DATE_SETTINGS: dict[str, Any] = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DAY_OF_MONTH": "first",
    "STRICT_PARSING": False,
}


@dataclass(frozen=True)
class StructuredArticle:
    """Article fields extracted from publisher-supplied structured data.

    Every field is optional — extraction returns whatever was found.
    ``raw`` carries the original JSON-LD blob (if any) so downstream
    stages can mine additional context (e.g. ``keywords``, ``about``)
    without re-parsing the HTML.
    """

    title: str | None = None
    description: str | None = None
    date_published: datetime | None = None
    date_modified: datetime | None = None
    author: str | None = None
    publisher: str | None = None
    article_body: str | None = None
    raw: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        """True when no field was populated — caller can skip persisting."""
        return not any(
            (
                self.title,
                self.description,
                self.date_published,
                self.date_modified,
                self.author,
                self.publisher,
                self.article_body,
            )
        )

    def merged_with_meta(self, meta: "StructuredArticle") -> "StructuredArticle":
        """Return a copy preferring our values, falling back to ``meta``."""
        return StructuredArticle(
            title=self.title or meta.title,
            description=self.description or meta.description,
            date_published=self.date_published or meta.date_published,
            date_modified=self.date_modified or meta.date_modified,
            author=self.author or meta.author,
            publisher=self.publisher or meta.publisher,
            article_body=self.article_body or meta.article_body,
            raw=self.raw or meta.raw,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    return dateparser.parse(value.strip(), settings=_DATE_SETTINGS)


def _coerce_str(value: Any) -> str | None:
    """Pull a plain string out of values that might be dict/list/str.

    Schema.org commonly nests names inside objects:
        {"author": {"@type": "Person", "name": "Jane Doe"}}
        {"author": [{"name": "A"}, {"name": "B"}]}
        {"publisher": {"name": "DCCEEW"}}
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("@id")
        return _coerce_str(name)
    if isinstance(value, list):
        names = [_coerce_str(item) for item in value]
        kept = [n for n in names if n]
        if not kept:
            return None
        return ", ".join(kept[:3])  # cap to avoid noisy 20-author blobs
    return None


def _flatten_jsonld(node: Any) -> list[dict[str, Any]]:
    """Walk a JSON-LD document and return every dict node it contains.

    Handles the three common shapes:
      • a single dict
      • a list of dicts
      • a dict with ``@graph`` containing a list of dicts
    """
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                out.extend(_flatten_jsonld(child))
    elif isinstance(node, list):
        for child in node:
            out.extend(_flatten_jsonld(child))
    return out


def _is_article_node(node: dict[str, Any]) -> bool:
    """True if a JSON-LD node's @type indicates it's an article."""
    type_field = node.get("@type")
    if isinstance(type_field, str):
        return type_field in _ARTICLE_TYPES
    if isinstance(type_field, list):
        return any(t in _ARTICLE_TYPES for t in type_field if isinstance(t, str))
    return False


# ---------------------------------------------------------------------------
# JSON-LD path
# ---------------------------------------------------------------------------


def _from_jsonld(tree: HTMLParser) -> StructuredArticle:
    """Parse all ``<script type="application/ld+json">`` blocks."""
    candidates: list[dict[str, Any]] = []

    for script in tree.css('script[type="application/ld+json"]'):
        raw_text = (script.text() or "").strip()
        if not raw_text:
            continue
        try:
            doc = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        candidates.extend(_flatten_jsonld(doc))

    # Prefer Article-typed nodes; fall back to first node if none match
    article_nodes = [n for n in candidates if _is_article_node(n)]
    pick = article_nodes[0] if article_nodes else (candidates[0] if candidates else None)
    if pick is None:
        return StructuredArticle()

    return StructuredArticle(
        title=_coerce_str(pick.get("headline") or pick.get("name")),
        description=_coerce_str(pick.get("description")),
        date_published=_parse_date(pick.get("datePublished") or pick.get("dateCreated")),
        date_modified=_parse_date(pick.get("dateModified")),
        author=_coerce_str(pick.get("author")),
        publisher=_coerce_str(pick.get("publisher")),
        article_body=_coerce_str(pick.get("articleBody")),
        raw=pick,
    )


# ---------------------------------------------------------------------------
# Meta-tag path
# ---------------------------------------------------------------------------


def _meta_content(tree: HTMLParser, *selectors: str) -> str | None:
    """Return the ``content`` attribute of the first matching ``<meta>`` tag."""
    for sel in selectors:
        for node in tree.css(sel):
            content = (cast(dict[str, str], node.attrs).get("content") or "").strip()
            if content:
                return content
    return None


def _from_meta(tree: HTMLParser) -> StructuredArticle:
    """Read Open Graph / Twitter / standard meta tags."""
    title = _meta_content(
        tree,
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        'meta[name="title"]',
    )
    if not title:
        title_node = tree.css_first("title")
        if title_node:
            title = title_node.text(strip=True) or None

    description = _meta_content(
        tree,
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
        'meta[name="description"]',
    )

    date_published_str = _meta_content(
        tree,
        'meta[property="article:published_time"]',
        'meta[property="og:article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="DC.date.issued"]',
        'meta[name="dcterms.issued"]',
        'meta[name="pubdate"]',
        'meta[name="publishdate"]',
        'meta[itemprop="datePublished"]',
    )
    date_modified_str = _meta_content(
        tree,
        'meta[property="article:modified_time"]',
        'meta[property="og:updated_time"]',
        'meta[name="article:modified_time"]',
        'meta[name="DC.date.modified"]',
        'meta[itemprop="dateModified"]',
    )

    author = _meta_content(
        tree,
        'meta[name="author"]',
        'meta[property="article:author"]',
        'meta[name="DC.creator"]',
    )
    publisher = _meta_content(
        tree,
        'meta[property="og:site_name"]',
        'meta[name="publisher"]',
        'meta[name="DC.publisher"]',
    )

    return StructuredArticle(
        title=title,
        description=description,
        date_published=_parse_date(date_published_str),
        date_modified=_parse_date(date_modified_str),
        author=author,
        publisher=publisher,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_structured(html: str) -> StructuredArticle | None:
    """Extract structured article metadata from raw HTML.

    Returns ``None`` if no useful data was found — callers should fall
    back to trafilatura. Otherwise returns a ``StructuredArticle`` with
    JSON-LD fields preferred and meta tags as fallbacks.
    """
    if not html:
        return None
    try:
        tree = HTMLParser(html)
    except Exception as exc:  # noqa: BLE001 — selectolax can raise odd errors
        logger.debug("structured extract: HTML parse failed", error=str(exc))
        return None

    jsonld = _from_jsonld(tree)
    meta = _from_meta(tree)
    merged = jsonld.merged_with_meta(meta)
    return None if merged.is_empty() else merged
