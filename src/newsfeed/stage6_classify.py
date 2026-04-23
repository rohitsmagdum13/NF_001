"""Stage 6 — LLM classification (section + jurisdiction + content_type).

Inputs
------
    SQLite: candidates table    (status='fetched')
    SQLite: context_bundles     (main_text per candidate)
    Disk:   data/source_registry.json (examples for few-shot prompting)

Outputs
-------
    SQLite: candidates table    (status → 'classified' or 'review')
    SQLite: drafts table        (new row per classified candidate)

What this stage does
--------------------
    For each ``status='fetched'`` candidate, builds a classification prompt
    from the article text, source hints, and few-shot examples, then calls
    the LLM (quality tier).

    * High confidence (≥ threshold)  → creates Draft row; status → 'classified'.
    * Low confidence (< threshold)   → status → 'review' (human fallback).
    * LLM error / empty text         → status → 'review'.

Prompt
------
    Template: ``src/newsfeed/prompts/classify_v1.j2``
    Version:  ``classify_v1``

Run directly
------------
    uv run python -m newsfeed.stage6_classify
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.llm_client import LLMClient, LLMStructuredOutputError
from newsfeed.schemas import ExampleTemplate, SourceRegistry

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_VERSION = "classify_v1"
_MAX_TEXT_CHARS = 3_000
_MAX_EXAMPLES = 4
_SYSTEM_PROMPT = (
    "You are a classification expert for an Australian water sector regulatory newsfeed. "
    "Assign each article to the correct section, jurisdiction, and content type. "
    "Be precise — confidence should reflect genuine certainty, not default to a high value."
)


# ---------------------------------------------------------------------------
# LLM response schema
# ---------------------------------------------------------------------------


class ClassifyResult(BaseModel):
    """LLM response for article classification."""

    model_config = ConfigDict(extra="ignore")

    section: str
    jurisdiction: str
    confidence: float = Field(ge=0.0, le=1.0)
    content_type: str

    @field_validator("section")
    @classmethod
    def _valid_section(cls, v: str) -> str:
        settings = get_settings()
        if settings.sections and v not in settings.sections:
            raise ValueError(f"Unknown section: {v!r}")
        return v

    @field_validator("jurisdiction")
    @classmethod
    def _valid_jurisdiction(cls, v: str) -> str:
        settings = get_settings()
        if settings.jurisdictions and v not in settings.jurisdictions:
            raise ValueError(f"Unknown jurisdiction: {v!r}")
        return v

    @field_validator("content_type")
    @classmethod
    def _valid_content_type(cls, v: str) -> str:
        settings = get_settings()
        if settings.content_types and v not in settings.content_types:
            raise ValueError(f"Unknown content_type: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


def _load_registry() -> SourceRegistry:
    settings = get_settings()
    registry_path = settings.paths.source_registry
    if not registry_path.exists():
        return SourceRegistry()
    return SourceRegistry.model_validate(json.loads(registry_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(
    article_text: str,
    section_hint: str,
    jurisdiction_hint: str,
    examples: list[ExampleTemplate],
) -> str:
    settings = get_settings()
    env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("classify_v1.j2")
    return template.render(
        article_text=article_text[:_MAX_TEXT_CHARS],
        section_hint=section_hint,
        jurisdiction_hint=jurisdiction_hint,
        sections=settings.sections,
        jurisdictions=settings.jurisdictions,
        examples=examples[:_MAX_EXAMPLES],
    )


# ---------------------------------------------------------------------------
# Per-candidate processing
# ---------------------------------------------------------------------------


def _classify_one(
    candidate: Candidate,
    bundle: ContextBundle,
    examples: list[ExampleTemplate],
    llm: LLMClient,
    run_id: str,
) -> ClassifyResult | None:
    """Classify one candidate. Returns None on LLM failure or empty text."""
    article_text = bundle.main_text or ""
    if not article_text.strip():
        logger.warning("empty bundle text — skipping classify", candidate_id=candidate.id)
        return None

    prompt = _build_prompt(
        article_text=article_text,
        section_hint=candidate.section_hint or "",
        jurisdiction_hint=candidate.jurisdiction_hint or "",
        examples=examples,
    )

    try:
        return llm.complete(
            prompt=prompt,
            schema=ClassifyResult,
            model_tier="quality",
            system=_SYSTEM_PROMPT,
            stage="stage6_classify",
            candidate_id=candidate.id,
            prompt_version=_PROMPT_VERSION,
            run_id=run_id,
        )
    except LLMStructuredOutputError as exc:
        logger.error(
            "LLM failed for classify — routing to review",
            candidate_id=candidate.id,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(run_id: str | None = None) -> dict[str, int]:
    """Run classification over all ``status='fetched'`` candidates.

    Returns ``{"checked": N, "classified": M, "review": R}``.
    """
    run_id = run_id or uuid.uuid4().hex
    settings = get_settings()
    llm = LLMClient()
    registry = _load_registry()

    with get_session() as session:
        rows: list[tuple[Candidate, ContextBundle]] = list(
            session.execute(
                select(Candidate, ContextBundle)
                .join(ContextBundle, Candidate.id == ContextBundle.candidate_id)
                .where(Candidate.status == "fetched")
            ).all()
        )

    if not rows:
        logger.info("classify: no fetched candidates")
        return {"checked": 0, "classified": 0, "review": 0}

    logger.info("classify start", candidates=len(rows))

    classified_count = 0
    review_count = 0
    threshold = settings.pipeline.confidence_threshold

    for candidate, bundle in rows:
        result = _classify_one(candidate, bundle, registry.examples, llm, run_id)

        if result is None or result.confidence < threshold:
            with get_session() as session:
                cand = session.get(Candidate, candidate.id)
                if cand:
                    cand.status = "review"
            review_count += 1
            if result is not None:
                logger.info(
                    "classify: low confidence — routing to review",
                    candidate_id=candidate.id,
                    confidence=result.confidence,
                )
            continue

        with get_session() as session:
            session.add(
                Draft(
                    candidate_id=candidate.id,
                    content_type=result.content_type,
                    section=result.section,
                    jurisdiction=result.jurisdiction,
                    confidence=result.confidence,
                    editor_decision="pending",
                )
            )
            cand = session.get(Candidate, candidate.id)
            if cand:
                cand.status = "classified"

        classified_count += 1
        logger.info(
            "classified",
            candidate_id=candidate.id,
            section=result.section,
            jurisdiction=result.jurisdiction,
            content_type=result.content_type,
            confidence=result.confidence,
        )

    counts = {"checked": len(rows), "classified": classified_count, "review": review_count}
    logger.info("classify complete", **counts)
    return counts


if __name__ == "__main__":
    run()
