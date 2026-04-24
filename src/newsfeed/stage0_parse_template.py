"""Stage 0 — parse the template file into a source registry.

Inputs
------
    reference/Water_Newsfeed_Template.md  (read-only ground truth)

Outputs
-------
    data/source_registry.json            (SourceRegistry payload)

The template has a stable shape (see ``reference/Water_Newsfeed_Template.md``):

    ## <n>. <Section>
    ### <Jurisdiction> - <Section>
    **<state abbrev OR example title>**
    - https://url-a
    - https://url-b

Parse rules
-----------
    * ``## N. Name`` where Name matches a configured section → sets
      ``current_section``. Sections 10 (Upcoming Dates) and 11 (Previous
      Newsfeeds) fall outside the whitelist and are skipped.
    * ``### Jurisdiction - Section`` where Jurisdiction matches a configured
      jurisdiction → sets ``current_jurisdiction``. "International" and
      anything else outside the whitelist is skipped.
    * A paragraph containing only ``**text**`` where text is a known state
      abbreviation (Vic / NSW / Qld / WA / SA / Tas / ACT / NT) → sets
      ``source_label`` on subsequent URLs.
    * Any other paragraph containing only ``**text**`` begins an
      ``ExampleTemplate``. The following paragraph(s) form the body;
      a trailing ``(Source: ...)`` is captured as ``source_attribution``.
    * URLs appear as plain text inside bullet list items. We extract them
      with regex from the inline token content and attach them to the
      current (section, jurisdiction, source_label).

Failure modes
-------------
    * Template file missing → ``FileNotFoundError``.
    * Empty sections / jurisdictions configured → ``ValueError``.
    * No URLs found → returns empty registry (logged at WARNING).

Run directly
------------
    uv run python -m newsfeed.stage0_parse_template
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import dateparser
import yaml
from loguru import logger
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdownify import markdownify as _html_to_md
from selectolax.parser import HTMLParser

from newsfeed.config import get_settings
from newsfeed.schemas import ExampleTemplate, SourceEntry, SourceRegistry

# --- Regexes ---

URL_RE = re.compile(r"https?://[^\s<>)\]]+")
SECTION_HEADING_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
BOLD_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
SOURCE_ATTRIBUTION_RE = re.compile(r"^\*?\(\s*Source[s]?\s*:\s*(.+?)\s*\)\*?\s*$", re.IGNORECASE)
_TXT_LINE_RE = re.compile(r"https?://[^\s<>)\]\"']+")
_HINT_RE = re.compile(r"^([^_].+?)__([^_].+?)__(.+)$")

# URL trailing punctuation we should strip (markdown often swallows a ).,;:)
_URL_TRAILING_PUNCT = ".,;:)"

STATE_ABBREVS: frozenset[str] = frozenset({"VIC", "NSW", "QLD", "WA", "SA", "TAS", "ACT", "NT"})

# Extensions we treat as HTML when loading the template file. HTML templates are
# converted to Markdown via markdownify before being handed to markdown-it-py —
# this keeps the existing token state machine unchanged.
_HTML_SUFFIXES: frozenset[str] = frozenset({".html", ".htm"})

# Template discovery. Preferred extensions in order — markdown first (native,
# no conversion step), then HTML. The glob fallback picks any file with these
# extensions in reference/, excluding known non-template files.
_TEMPLATE_EXTENSIONS: tuple[str, ...] = (".md", ".html", ".htm")

# Files in reference/ that are NOT URL-registry templates and must not be parsed
# as one. ``Water_Newsfeed.md`` is the OUTPUT format exemplar (used by prompts).
_NON_TEMPLATE_FILENAMES: frozenset[str] = frozenset({"water_newsfeed.md"})

# Canonical template base name — checked first for backward compatibility
# before the glob discovery fallback kicks in.
_CANONICAL_TEMPLATE_STEM: str = "Water_Newsfeed_Template"


# --- Parser state ---


@dataclass
class _ParserState:
    section: str | None = None
    jurisdiction: str | None = None
    source_label: str | None = None
    example_title: str | None = None
    example_body: list[str] = field(default_factory=list)
    example_source: str | None = None

    def flush_example(self, out: list[ExampleTemplate]) -> None:
        # Only emit when we actually captured body text. Bare bold labels
        # like "**Reference Links:**" or "**EPBC Act Referrals Procedure:**"
        # are section dividers, not draft exemplars.
        if self.example_title and self.section and self.jurisdiction:
            body = " ".join(self.example_body).strip()
            if body:
                out.append(
                    ExampleTemplate(
                        section=self.section,
                        jurisdiction=self.jurisdiction,
                        title=self.example_title,
                        body=body,
                        source_attribution=self.example_source,
                    )
                )
        self.example_title = None
        self.example_body = []
        self.example_source = None


# --- Helpers ---


def _slug(text: str, max_len: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:max_len] or "x"


def _source_id(section: str, jurisdiction: str, url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"{_slug(section, 20)}__{_slug(jurisdiction, 10)}__{digest}"


def _heading_level(tok: Token) -> int | None:
    if tok.type != "heading_open":
        return None
    try:
        return int(tok.tag.lstrip("h"))
    except ValueError:
        return None


def _clean_url(raw: str) -> str:
    return raw.rstrip(_URL_TRAILING_PUNCT)


def _discover_template_path(ref_dir: Path) -> Path | None:
    """Locate the URL-registry template inside ``ref_dir``.

    Resolution order (first match wins):

      1. Canonical name ``Water_Newsfeed_Template.{md,html,htm}`` — preserved
         for backward compatibility with the committed reference/.
      2. Any other ``*.md`` / ``*.html`` / ``*.htm`` file in ``ref_dir``,
         excluding known non-template files like ``Water_Newsfeed.md``
         (which is the output-format exemplar, not a source list).

    Returns ``None`` when nothing suitable is found; the caller then
    degrades gracefully to a ``local_articles``-only registry.

    Extensions are tried in ``_TEMPLATE_EXTENSIONS`` order — markdown first
    (native, no conversion), then HTML. Within an extension, files are
    sorted alphabetically for deterministic results across machines.
    """
    if not ref_dir.exists():
        logger.warning("reference directory not found", path=str(ref_dir))
        return None

    # 1. Canonical path — fast path, preserves pre-existing behaviour.
    for ext in _TEMPLATE_EXTENSIONS:
        canonical = ref_dir / f"{_CANONICAL_TEMPLATE_STEM}{ext}"
        if canonical.exists():
            return canonical

    # 2. Glob fallback — any template-shaped file, excluding known non-templates.
    matches: list[Path] = []
    for ext in _TEMPLATE_EXTENSIONS:
        for path in sorted(ref_dir.glob(f"*{ext}")):
            if path.name.lower() in _NON_TEMPLATE_FILENAMES:
                continue
            matches.append(path)

    if not matches:
        return None

    chosen = matches[0]
    if len(matches) > 1:
        logger.warning(
            "multiple template candidates found in reference/ — using first",
            chosen=str(chosen),
            others=[str(p) for p in matches[1:]],
        )
    else:
        logger.info("discovered template via glob", path=str(chosen))
    return chosen


def _load_template_as_markdown(path: Path) -> str:
    """Load the template file as markdown text.

    Markdown files are returned verbatim. HTML files are converted via
    ``markdownify`` so headings, bold labels, bullet lists, and link URLs
    map cleanly onto the markdown-it token stream the parser already
    understands.

    Heading style ``ATX`` produces ``## Section`` / ``### Jurisdiction``
    (not SETEXT underlines), which is what ``SECTION_HEADING_RE`` expects.
    Scripts and styles are stripped to avoid noise tokens in the output.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in _HTML_SUFFIXES:
        md_text: str = _html_to_md(text, heading_style="ATX", strip=["script", "style"])
        logger.info(
            "converted HTML template to markdown",
            path=str(path),
            html_bytes=len(text),
            md_bytes=len(md_text),
        )
        return md_text
    return text


# --- Public API ---


def parse_template(  # noqa: PLR0912, PLR0915 — token state machine, splitting hurts clarity
    template_path: Path,
    *,
    valid_sections: set[str] | None = None,
    valid_jurisdictions: set[str] | None = None,
) -> SourceRegistry:
    """Parse the water-newsfeed template file.

    Args:
        template_path: path to ``Water_Newsfeed_Template.md``.
        valid_sections: override for allowed sections. Defaults to
            ``settings.sections``.
        valid_jurisdictions: override for allowed jurisdictions. Defaults to
            ``settings.jurisdictions``.
    """
    settings = get_settings()
    sections = valid_sections if valid_sections is not None else set(settings.sections)
    jurisdictions = (
        valid_jurisdictions if valid_jurisdictions is not None else set(settings.jurisdictions)
    )
    if not sections or not jurisdictions:
        raise ValueError("settings.sections and settings.jurisdictions must be populated")

    text = _load_template_as_markdown(template_path)
    tokens = MarkdownIt().parse(text)

    sources: list[SourceEntry] = []
    examples: list[ExampleTemplate] = []
    state = _ParserState()

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # --- Headings (h2 = section, h3 = jurisdiction) ---
        level = _heading_level(tok)
        if level in (2, 3) and i + 1 < len(tokens):
            inline = tokens[i + 1]
            content = inline.content.strip()

            if level == 2:
                state.flush_example(examples)
                m = SECTION_HEADING_RE.match(content)
                candidate = m.group(2).strip() if m else ""
                state.section = candidate if candidate in sections else None
                state.jurisdiction = None
                state.source_label = None
            else:  # level == 3
                state.flush_example(examples)
                jur = content.split(" - ", 1)[0].strip() if " - " in content else ""
                state.jurisdiction = jur if jur in jurisdictions else None
                state.source_label = None

            i += 3  # heading_open, inline, heading_close
            continue

        # --- Paragraphs ---
        if tok.type == "paragraph_open" and i + 1 < len(tokens):
            inline = tokens[i + 1]
            content = inline.content.strip()

            # Case 1: paragraph is just `**something**`
            bold_m = BOLD_ONLY_RE.match(content)
            if bold_m:
                label = bold_m.group(1).strip()
                state.flush_example(examples)
                if label.upper() in STATE_ABBREVS:
                    state.source_label = label
                else:
                    state.example_title = label
                i += 3
                continue

            # Case 2: inside an example — capture source attribution or body
            if state.example_title:
                src_m = SOURCE_ATTRIBUTION_RE.match(content)
                if src_m:
                    state.example_source = src_m.group(1).strip()
                elif content:
                    state.example_body.append(content)

            i += 3
            continue

        # --- Bullet lists: collect URLs from inline tokens ---
        if tok.type == "bullet_list_open":
            state.flush_example(examples)

            depth = 1
            j = i + 1
            while j < len(tokens) and depth > 0:
                inner = tokens[j]
                if inner.type == "bullet_list_open":
                    depth += 1
                elif inner.type == "bullet_list_close":
                    depth -= 1
                    if depth == 0:
                        break
                elif inner.type == "inline" and state.section and state.jurisdiction:
                    for raw_url in URL_RE.findall(inner.content):
                        url = _clean_url(raw_url)
                        sources.append(
                            SourceEntry(
                                source_id=_source_id(state.section, state.jurisdiction, url),
                                source_type="url",
                                url=url,
                                section=state.section,
                                jurisdiction=state.jurisdiction,
                                source_label=state.source_label,
                            )
                        )
                j += 1
            i = j + 1
            continue

        # --- Horizontal rule ends any in-progress example ---
        if tok.type == "hr":
            state.flush_example(examples)

        i += 1

    state.flush_example(examples)

    # Dedup by (section, jurisdiction, url). Template is handwritten and
    # occasionally lists the same URL under different labels.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[SourceEntry] = []
    for s in sources:
        key = (s.section, s.jurisdiction, s.url or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    if not deduped:
        logger.warning("no URLs extracted from template", path=str(template_path))
    else:
        logger.info(
            "parsed template",
            sources=len(deduped),
            examples=len(examples),
            path=str(template_path),
        )
    return SourceRegistry(sources=deduped, examples=examples)


def _hints_from_stem(
    stem: str,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> tuple[str | None, str | None]:
    """Extract section and jurisdiction from a filename stem like Section__Jurisdiction__title."""
    m = _HINT_RE.match(stem)
    if not m:
        return None, None
    raw_section = m.group(1).replace("_", " ")
    raw_juris = m.group(2).replace("_", " ")
    section = next((s for s in valid_sections if s.lower() == raw_section.lower()), None)
    juris = next((j for j in valid_jurisdictions if j.lower() == raw_juris.lower()), None)
    return section, juris


def _entries_from_txt_file(
    path: Path,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> list[SourceEntry]:
    """Parse a .txt file into one SourceEntry per URL."""
    section_hint, juris_hint = _hints_from_stem(path.stem, valid_sections, valid_jurisdictions)
    section = section_hint or valid_sections[0]
    jurisdiction = juris_hint or valid_jurisdictions[0]

    entries: list[SourceEntry] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for raw_url in _TXT_LINE_RE.findall(line):
            url = raw_url.rstrip(".,;:)")
            if not url:
                continue
            entries.append(
                SourceEntry(
                    source_id=_source_id(section, jurisdiction, url),
                    source_type="url",
                    url=url,
                    section=section,
                    jurisdiction=jurisdiction,
                    source_label=path.stem,
                )
            )
    return entries


def _build_yaml_entry(
    item: dict[str, object],
    default_section: str | None,
    default_jurisdiction: str | None,
    path: Path,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> SourceEntry | None:
    """Construct a SourceEntry from one YAML mapping, or None if invalid.

    Resolution for section/jurisdiction (first non-empty wins):
      1. entry-level ``section`` / ``jurisdiction``
      2. ``default_section`` / ``default_jurisdiction`` (group or filename-derived)
      3. ``valid_sections[0]`` / ``valid_jurisdictions[0]`` (last-resort fallback)

    Invalid section or jurisdiction values trigger a WARNING and the entry
    is skipped — prevents a typo like ``Water Reforms`` from silently
    polluting the registry with an untracked section.
    """
    url_raw = item.get("url")
    if not isinstance(url_raw, str):
        return None
    url = url_raw.strip().rstrip(".,;:)")
    if not url:
        return None

    section = str(item.get("section") or default_section or valid_sections[0])
    jurisdiction = str(item.get("jurisdiction") or default_jurisdiction or valid_jurisdictions[0])

    if section not in valid_sections:
        logger.warning(
            "yaml entry has invalid section — skipped",
            path=str(path), url=url, section=section,
        )
        return None
    if jurisdiction not in valid_jurisdictions:
        logger.warning(
            "yaml entry has invalid jurisdiction — skipped",
            path=str(path), url=url, jurisdiction=jurisdiction,
        )
        return None

    sublink_patterns_raw = item.get("sublink_patterns") or []
    sublink_patterns = (
        [str(p) for p in sublink_patterns_raw if isinstance(p, str)]
        if isinstance(sublink_patterns_raw, list)
        else []
    )

    return SourceEntry(
        source_id=_source_id(section, jurisdiction, url),
        source_type="url",
        url=url,
        section=section,
        jurisdiction=jurisdiction,
        source_label=str(item.get("source_label") or path.stem),
        requires_js=bool(item.get("requires_js", False)),
        sublink_patterns=sublink_patterns,
    )


def _entries_from_yaml_file(
    path: Path,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> list[SourceEntry]:
    """Parse a .yaml/.yml file with per-URL source configuration.

    Two structural forms are supported:

    (1) **Flat** — all entries share the filename-derived section/jurisdiction::

            # filename: Section__Jurisdiction__slug.yaml
            sources:
              - url: https://example.gov.au/news
                requires_js: true

    (2) **Grouped** — one file with multiple (section, jurisdiction) groups::

            groups:
              - section: Water Reform
                jurisdiction: Victoria
                sources:
                  - url: https://example1.vic.gov.au/
                  - url: https://example2.vic.gov.au/
                    requires_js: true
              - section: Water Rights
                jurisdiction: National
                sources:
                  - url: https://example.gov.au/

    Any entry may override with its own ``section`` / ``jurisdiction``.

    Per-entry fields:
        url               (required)
        requires_js       (optional, bool; default false)
        sublink_patterns  (optional, list[regex]; default [])
        source_label      (optional, str; defaults to filename stem)
        section           (optional, str; overrides group/filename defaults)
        jurisdiction      (optional, str; overrides group/filename defaults)

    Malformed files log a WARNING and return ``[]``. Entries missing a
    valid ``url`` are silently skipped.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.warning("yaml parse failed", path=str(path), error=str(exc))
        return []

    if not isinstance(data, dict):
        logger.warning(
            "yaml file must be a mapping with 'sources' or 'groups'",
            path=str(path),
        )
        return []

    filename_section, filename_juris = _hints_from_stem(
        path.stem, valid_sections, valid_jurisdictions
    )
    entries: list[SourceEntry] = []

    # Form 2 — grouped
    groups_raw = data.get("groups")
    if isinstance(groups_raw, list):
        for grp in groups_raw:
            if not isinstance(grp, dict):
                continue
            grp_section = str(grp.get("section") or "") or filename_section
            grp_juris = str(grp.get("jurisdiction") or "") or filename_juris
            grp_sources = grp.get("sources")
            if not isinstance(grp_sources, list):
                continue
            for item in grp_sources:
                if not isinstance(item, dict):
                    continue
                entry = _build_yaml_entry(
                    item, grp_section, grp_juris,
                    path, valid_sections, valid_jurisdictions,
                )
                if entry is not None:
                    entries.append(entry)
        return entries

    # Form 1 — flat
    sources_raw = data.get("sources")
    if isinstance(sources_raw, list):
        for item in sources_raw:
            if not isinstance(item, dict):
                continue
            entry = _build_yaml_entry(
                item, filename_section, filename_juris,
                path, valid_sections, valid_jurisdictions,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    logger.warning(
        "yaml file must have 'groups' or 'sources' at top level",
        path=str(path),
    )
    return entries


# --- Robust metadata extraction for messy local files ---

_DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)
_MD_HEADING_RE = re.compile(r"^\s*#{1,3}\s+(.+?)\s*$", re.MULTILINE)


# Generic/placeholder titles sometimes left in <title> tags — skip these in favour
# of structured metadata (og:title, h1) when available.
_GENERIC_TITLES = frozenset({
    "untitled page", "untitled", "home", "welcome", "page", "index",
    "document", "new page", "no title", "error", "not found", "404",
})


def _is_generic_title(text: str) -> bool:
    """True if text looks like a placeholder title rather than a real article title."""
    return text.strip().lower() in _GENERIC_TITLES


def _extract_html_title(tree: HTMLParser) -> str | None:
    """Try multiple strategies to find a title in messy HTML.

    Priority (most → least reliable):
      1. <h1> (when present, the clearest signal of article title)
      2. Open Graph / Twitter meta (CMS platforms set these for social sharing)
      3. <title> (often truncated/generic; skipped if in the 'generic' blocklist)
      4. <h2>
    """
    # 1. <h1>
    for node in tree.css("h1"):
        t = node.text(strip=True)
        if t and len(t) >= 5:
            return t
    # 2. Open Graph / Twitter meta — preferred over <title> because CMS platforms
    #    set these precisely for social sharing (accurate even when <title> is generic)
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        nodes = tree.css(sel)
        if nodes:
            content = (nodes[0].attrs or {}).get("content", "").strip()
            if content and not _is_generic_title(content):
                return content
    # 3. <title> — only if non-generic
    for node in tree.css("title"):
        t = node.text(strip=True)
        if t and len(t) >= 5 and not _is_generic_title(t):
            return t
    # 4. <h2> — fallback for pages missing h1
    for node in tree.css("h2"):
        t = node.text(strip=True)
        if t and len(t) >= 5:
            return t
    return None


def _extract_html_date(tree: HTMLParser, body_text: str) -> str | None:
    """Try multiple strategies to find a publication date. Returns ISO string or None."""
    # 1. <time datetime="...">
    for node in tree.css("time[datetime]"):
        raw = (node.attrs or {}).get("datetime", "").strip()
        if raw:
            parsed = dateparser.parse(raw)
            if parsed:
                return parsed.date().isoformat()
    # 2. Meta tags used by CMS platforms
    meta_selectors = (
        'meta[property="article:published_time"]',
        'meta[name="pubdate"]',
        'meta[name="publishdate"]',
        'meta[name="date"]',
        'meta[itemprop="datePublished"]',
    )
    for sel in meta_selectors:
        nodes = tree.css(sel)
        if nodes:
            content = (nodes[0].attrs or {}).get("content", "").strip()
            parsed = dateparser.parse(content) if content else None
            if parsed:
                return parsed.date().isoformat()
    # 3. First date-shaped text inside the first 2000 chars of visible body text
    m = _DATE_TEXT_RE.search(body_text[:2000])
    if m:
        parsed = dateparser.parse(m.group(0))
        if parsed:
            return parsed.date().isoformat()
    return None


def _extract_md_title(text: str) -> str | None:
    """First markdown heading (#, ##, ###) with meaningful text."""
    for m in _MD_HEADING_RE.finditer(text):
        title = m.group(1).strip()
        if len(title) >= 5:
            return title
    # Fall back to first non-empty line
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and len(stripped) >= 5:
            return stripped
    return None


def _parse_local_file_metadata(path: Path) -> tuple[str | None, str | None]:
    """Return (title_hint, pub_date_hint) for a local .html/.htm/.md file.

    Uses progressive fallbacks so messy or sparsely-structured files still
    yield usable metadata. Logs a WARNING when nothing can be extracted.
    """
    suffix = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("local file read failed", path=str(path), error=str(exc))
        return None, None

    if not content.strip():
        logger.warning("local file is empty", path=str(path))
        return None, None

    if suffix in (".html", ".htm"):
        try:
            tree = HTMLParser(content)
        except Exception as exc:
            logger.warning("HTML parse failed", path=str(path), error=str(exc))
            return None, None
        body_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
        title = _extract_html_title(tree)
        pub_date = _extract_html_date(tree, body_text)
    else:  # .md
        title = _extract_md_title(content)
        m = _DATE_TEXT_RE.search(content[:2000])
        pub_date = (dateparser.parse(m.group(0)).date().isoformat() if m and dateparser.parse(m.group(0)) else None)

    if title is None and pub_date is None:
        logger.warning(
            "local file has no extractable title or date — will flag for editor review",
            path=str(path),
        )
    elif title is None:
        logger.warning("local file missing title", path=str(path))
    elif pub_date is None:
        logger.warning("local file missing publication date", path=str(path))

    return title, pub_date


def _entry_from_local_file(
    path: Path,
    valid_sections: list[str],
    valid_jurisdictions: list[str],
) -> SourceEntry | None:
    """Build a SourceEntry for a local .html/.htm/.md file with metadata hints."""
    suffix = path.suffix.lower()
    if suffix not in (".html", ".htm", ".md"):
        return None

    section_hint, juris_hint = _hints_from_stem(path.stem, valid_sections, valid_jurisdictions)
    source_type = "html" if suffix in (".html", ".htm") else "md"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]

    title_hint, pub_date_hint = _parse_local_file_metadata(path)

    return SourceEntry(
        source_id=f"local__{_slug(path.stem, 30)}__{digest}",
        source_type=source_type,
        local_path=str(path),
        section=section_hint or valid_sections[0],
        jurisdiction=juris_hint or valid_jurisdictions[0],
        source_label=path.stem,
        title_hint=title_hint,
        pub_date_hint=pub_date_hint,
    )


def _entries_from_local_articles(
    local_articles_path: Path,
    settings: object,
) -> list[SourceEntry]:
    """Scan data/local_articles/ and return a flat list of SourceEntry.

    Handles four file types:
      * .txt          → one entry per URL line (plain URL list)
      * .yaml / .yml  → richer per-URL config (requires_js, sublink_patterns, …)
      * .html, .htm   → single-article entry pointing at the file
      * .md           → single-article entry pointing at the file
    """
    if not local_articles_path.exists():
        logger.warning("local_articles folder not found", path=str(local_articles_path))
        return []

    valid_sections: list[str] = settings.sections  # type: ignore[attr-defined]
    valid_jurisdictions: list[str] = settings.jurisdictions  # type: ignore[attr-defined]

    entries: list[SourceEntry] = []
    for path in sorted(local_articles_path.iterdir()):
        if path.name.upper().startswith("README"):
            continue
        suffix = path.suffix.lower()

        if suffix == ".txt":
            entries.extend(_entries_from_txt_file(path, valid_sections, valid_jurisdictions))
        elif suffix in (".yaml", ".yml"):
            entries.extend(_entries_from_yaml_file(path, valid_sections, valid_jurisdictions))
        elif suffix in (".html", ".htm", ".md"):
            entry = _entry_from_local_file(path, valid_sections, valid_jurisdictions)
            if entry:
                entries.append(entry)

    logger.info(
        "scanned local_articles/",
        entries=len(entries),
        folder=str(local_articles_path),
    )
    return entries




def run(
    template_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, int]:
    """Entry point: build ``source_registry.json`` from ``data/local_articles/``.

    The source of truth is ``data/local_articles/`` — ``.txt`` URL lists,
    ``.yaml`` group files (with optional ``requires_js`` per entry), and
    ``.html`` / ``.md`` article files.

    The ``reference/`` folder is NOT scanned automatically. If a caller
    explicitly passes a ``template_path``, the referenced file is parsed
    (supports ``.md``, ``.html``, ``.htm``; HTML is converted via
    markdownify). Otherwise the registry is built solely from
    ``local_articles/``.

    Returns ``{"sources": N, "examples": M, "local": L}``.
    """
    settings = get_settings()
    output_path = output_path or settings.paths.source_registry

    # Explicit template_path wins — but we do NOT auto-discover from
    # reference/ anymore. local_articles/ is the authoritative source.
    if template_path is not None and template_path.exists():
        registry = parse_template(template_path)
    else:
        registry = SourceRegistry()

    # Always merge in local_articles/ entries (.txt URLs + .html/.md files)
    local_entries = _entries_from_local_articles(settings.paths.local_articles, settings)
    if local_entries:
        existing_keys: set[tuple[str, str, str]] = {
            (s.section, s.jurisdiction, s.url or s.local_path or "")
            for s in registry.sources
        }
        for entry in local_entries:
            key = (entry.section, entry.jurisdiction, entry.url or entry.local_path or "")
            if key not in existing_keys:
                registry.sources.append(entry)
                existing_keys.add(key)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(registry.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    counts = {
        "sources": len(registry.sources),
        "examples": len(registry.examples),
        "local": len(local_entries),
    }
    logger.info("wrote source registry", path=str(output_path), **counts)
    return counts


if __name__ == "__main__":
    run()
