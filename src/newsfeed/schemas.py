"""Pydantic models shared across pipeline stages.

Stage modules communicate through Pydantic models (never raw dicts) per the
coding standards in ``CLAUDE.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["url", "html", "md", "pdf"]


class SourceEntry(BaseModel):
    """One discovery source.

    Stage 1 iterates every ``SourceEntry`` in the registry. ``source_type``
    decides which fetch path runs (RSS/sitemap/HTML listing for URLs,
    direct parse for local HTML/MD, watcher for PDFs).
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: SourceType
    url: str | None = None
    local_path: str | None = None
    section: str
    jurisdiction: str
    source_label: str | None = None
    example_template: str | None = None
    requires_js: bool = False
    sublink_patterns: list[str] = Field(default_factory=list)
    # Pre-extracted metadata for local files — populated by Stage 0 so Stage 1
    # has fallbacks when the raw HTML/MD has poor or missing structure.
    title_hint: str | None = None
    pub_date_hint: str | None = None  # ISO 8601 date or datetime


class ExampleTemplate(BaseModel):
    """Example draft stub extracted from the template file.

    Used by Stage 6 (classification) and Stage 7 (drafting) as few-shot
    exemplars — showing the LLM what a well-formed draft for a given
    (section, jurisdiction) looks like.
    """

    model_config = ConfigDict(extra="forbid")

    section: str
    jurisdiction: str
    title: str
    body: str
    source_attribution: str | None = None


class SourceRegistry(BaseModel):
    """On-disk shape of ``data/source_registry.json``."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceEntry] = Field(default_factory=list)
    examples: list[ExampleTemplate] = Field(default_factory=list)


class AssembledEntry(BaseModel):
    """One approved or pending draft entry in the assembled newsfeed."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    draft_id: int
    section: str
    jurisdiction: str
    content_type: str
    draft_text: str
    source_url: str | None = None
    source_label: str | None = None


class AssembledSection(BaseModel):
    """All entries for one section, grouped by jurisdiction."""

    model_config = ConfigDict(extra="forbid")

    section: str
    entries: list[AssembledEntry] = Field(default_factory=list)


class AssembledNewsfeed(BaseModel):
    """Full assembled newsfeed — written to outputs/{date}/horizon_payload.json.

    Stage 11 reads this to render the final .docx.
    """

    model_config = ConfigDict(extra="forbid")

    run_date: str
    sections: list[AssembledSection] = Field(default_factory=list)
    upcoming_dates: str = ""
    previous_newsfeeds: str = ""
    copyright_footer: str = "© Legalwise Seminars Pty Ltd. All rights reserved."
