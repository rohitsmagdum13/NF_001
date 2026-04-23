"""Stage 7 — LLM draft generation.

Inputs
------
    SQLite: candidates table    (status='classified')
    SQLite: drafts table        (content_type, section, jurisdiction; draft_text IS NULL)
    SQLite: context_bundles     (main_text + sublinks_json per candidate)

Outputs
-------
    SQLite: drafts table        (draft_text populated)
    SQLite: candidates table    (status → 'drafted' or 'review')

What this stage does
--------------------
    For each ``status='classified'`` candidate whose Draft row has no draft_text yet,
    selects the matching format template from ``src/newsfeed/templates/``, builds
    a prompt embedding the template and article context, then calls the LLM
    (quality tier) to fill in the slots.

    * LLM success  → draft_text stored; candidate status → 'drafted'.
    * LLM error    → candidate status → 'review'.
    * Empty text   → candidate status → 'review' (no LLM call).

Prompt
------
    Template: ``src/newsfeed/prompts/draft_v1.j2``
    Version:  ``draft_v1``

Format templates
----------------
    ``src/newsfeed/templates/{content_type}.j2`` — plain-text format with {SLOT}
    placeholders shown to the LLM as the target structure.

Run directly
------------
    uv run python -m newsfeed.stage7_draft
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.llm_client import LLMClient, LLMStructuredOutputError

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_PROMPT_VERSION = "draft_v1"
_MAX_TEXT_CHARS = 4_000
_MAX_SUBLINKS_CHARS = 2_000
_VALID_CONTENT_TYPES = frozenset(
    {"new_plan", "media_release", "weekly_report", "bullet_list", "simple_update", "gazette_bundle"}
)
_SYSTEM_PROMPT = (
    "You are a skilled editor drafting regulatory newsfeed entries for an Australian water "
    "sector publication. Draft concisely and accurately, using only facts from the source. "
    "Every quoted phrase in '[...]' must appear verbatim in the provided article text."
)


# ---------------------------------------------------------------------------
# LLM response schema
# ---------------------------------------------------------------------------


class DraftResult(BaseModel):
    """LLM response for draft generation — the fully rendered entry text."""

    model_config = ConfigDict(extra="ignore")

    draft_text: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_format_template(content_type: str) -> str:
    template_file = _TEMPLATES_DIR / f"{content_type}.j2"
    if not template_file.exists():
        raise FileNotFoundError(
            f"No format template for content_type={content_type!r}: {template_file}"
        )
    return template_file.read_text(encoding="utf-8")


def _build_sublinks_text(sublinks_json: str | None) -> str:
    if not sublinks_json:
        return ""
    try:
        sublinks = json.loads(sublinks_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(sublinks, list):
        return ""
    parts: list[str] = []
    for item in sublinks:
        if isinstance(item, dict):
            url = item.get("url", "")
            text = item.get("text", "")
            if text:
                parts.append(f"[{url}]\n{text[:500]}")
    return "\n\n".join(parts)[:_MAX_SUBLINKS_CHARS]


def _build_prompt(
    article_text: str,
    section: str,
    jurisdiction: str,
    content_type: str,
    format_template: str,
    sublinks_text: str,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("draft_v1.j2")
    return template.render(
        article_text=article_text[:_MAX_TEXT_CHARS],
        section=section,
        jurisdiction=jurisdiction,
        content_type=content_type,
        format_template=format_template,
        sublinks_text=sublinks_text,
    )


# ---------------------------------------------------------------------------
# Per-candidate processing
# ---------------------------------------------------------------------------


def _draft_one(
    candidate: Candidate,
    bundle: ContextBundle,
    draft: Draft,
    llm: LLMClient,
    run_id: str,
) -> str | None:
    """Generate draft text for one candidate. Returns draft_text or None on failure."""
    article_text = bundle.main_text or ""
    if not article_text.strip():
        logger.warning("empty bundle — skipping draft", candidate_id=candidate.id)
        return None

    content_type = draft.content_type or "simple_update"
    if content_type not in _VALID_CONTENT_TYPES:
        logger.warning(
            "unknown content_type — falling back to simple_update",
            candidate_id=candidate.id,
            content_type=content_type,
        )
        content_type = "simple_update"

    try:
        format_template = _load_format_template(content_type)
    except FileNotFoundError as exc:
        logger.error("missing format template", error=str(exc))
        return None

    sublinks_text = _build_sublinks_text(bundle.sublinks_json)
    prompt = _build_prompt(
        article_text=article_text,
        section=draft.section or candidate.section_hint or "",
        jurisdiction=draft.jurisdiction or candidate.jurisdiction_hint or "",
        content_type=content_type,
        format_template=format_template,
        sublinks_text=sublinks_text,
    )

    try:
        result = llm.complete(
            prompt=prompt,
            schema=DraftResult,
            model_tier="quality",
            system=_SYSTEM_PROMPT,
            stage="stage7_draft",
            candidate_id=candidate.id,
            prompt_version=_PROMPT_VERSION,
            run_id=run_id,
        )
        return result.draft_text
    except LLMStructuredOutputError as exc:
        logger.error(
            "LLM failed for draft — routing to review",
            candidate_id=candidate.id,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(run_id: str | None = None) -> dict[str, int]:
    """Generate drafts for all ``status='classified'`` candidates.

    Returns ``{"checked": N, "drafted": M, "review": R}``.
    """
    run_id = run_id or uuid.uuid4().hex
    llm = LLMClient()

    with get_session() as session:
        rows: list[tuple[Candidate, ContextBundle, Draft]] = list(
            session.execute(
                select(Candidate, ContextBundle, Draft)
                .join(ContextBundle, Candidate.id == ContextBundle.candidate_id)
                .join(Draft, Candidate.id == Draft.candidate_id)
                .where(Candidate.status == "classified")
                .where(Draft.editor_decision == "pending")
                .where(Draft.draft_text.is_(None))
            ).all()
        )

    if not rows:
        logger.info("draft: no classified candidates awaiting drafts")
        return {"checked": 0, "drafted": 0, "review": 0}

    logger.info("draft start", candidates=len(rows))

    drafted_count = 0
    review_count = 0

    for candidate, bundle, draft in rows:
        draft_text = _draft_one(candidate, bundle, draft, llm, run_id)

        if draft_text is None:
            with get_session() as session:
                cand = session.get(Candidate, candidate.id)
                if cand:
                    cand.status = "review"
            review_count += 1
            continue

        with get_session() as session:
            d = session.get(Draft, draft.id)
            if d:
                d.draft_text = draft_text
            cand = session.get(Candidate, candidate.id)
            if cand:
                cand.status = "drafted"

        drafted_count += 1
        logger.info("drafted", candidate_id=candidate.id, draft_id=draft.id)

    counts = {"checked": len(rows), "drafted": drafted_count, "review": review_count}
    logger.info("draft complete", **counts)
    return counts


if __name__ == "__main__":
    run()
