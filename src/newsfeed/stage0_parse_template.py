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

from loguru import logger
from markdown_it import MarkdownIt
from markdown_it.token import Token

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

    text = template_path.read_text(encoding="utf-8")
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


def _registry_from_local_articles(local_articles_path: Path, settings: object) -> SourceRegistry:
    """Build a SourceRegistry by scanning .txt URL files in the local_articles folder."""
    if not local_articles_path.exists():
        logger.warning("local_articles folder not found", path=str(local_articles_path))
        return SourceRegistry()

    valid_sections: list[str] = settings.sections  # type: ignore[attr-defined]
    valid_jurisdictions: list[str] = settings.jurisdictions  # type: ignore[attr-defined]

    txt_files = [
        p for p in sorted(local_articles_path.iterdir())
        if p.suffix.lower() == ".txt" and not p.name.upper().startswith("README")
    ]
    if not txt_files:
        logger.warning("no .txt files in local_articles/", path=str(local_articles_path))
        return SourceRegistry()

    sources: list[SourceEntry] = []
    for path in txt_files:
        section_hint, juris_hint = _hints_from_stem(path.stem, valid_sections, valid_jurisdictions)
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for raw_url in _TXT_LINE_RE.findall(line):
                url = raw_url.rstrip(".,;:)")
                if not url:
                    continue
                sources.append(
                    SourceEntry(
                        source_id=_source_id(
                            section_hint or valid_sections[0],
                            juris_hint or valid_jurisdictions[0],
                            url,
                        ),
                        source_type="url",
                        url=url,
                        section=section_hint or valid_sections[0],
                        jurisdiction=juris_hint or valid_jurisdictions[0],
                        source_label=path.stem,
                    )
                )

    logger.info(
        "built registry from local_articles/",
        sources=len(sources),
        folder=str(local_articles_path),
    )
    return SourceRegistry(sources=sources)


def run(
    template_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, int]:
    """Entry point: parse template → write ``source_registry.json``.

    Falls back to scanning ``data/local_articles/`` when the template is absent.
    Returns a counts dict: ``{"sources": N, "examples": M}``.
    """
    settings = get_settings()
    template_path = template_path or (settings.paths.reference / "Water_Newsfeed_Template.md")
    output_path = output_path or settings.paths.source_registry

    if template_path.exists():
        registry = parse_template(template_path)
    else:
        logger.warning(
            "template not found — building registry from local_articles/ instead",
            template=str(template_path),
        )
        registry = _registry_from_local_articles(settings.paths.local_articles, settings)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(registry.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    counts = {"sources": len(registry.sources), "examples": len(registry.examples)}
    logger.info("wrote source registry", path=str(output_path), **counts)
    return counts


if __name__ == "__main__":
    run()
