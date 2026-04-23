"""Stage 5 — LLM relevance filter (binary gate).

Inputs
------
    SQLite: candidates table    (status='fetched')
    SQLite: context_bundles     (main_text per candidate)

Outputs
-------
    SQLite: candidates table    (status updated: 'fetched' stays, irrelevant → 'filtered',
                                 LLM failure → 'review')

What this stage does
--------------------
    For each ``status='fetched'`` candidate, builds a prompt from the article text
    and source context, calls the LLM (cheap tier), and applies the binary gate:

    * ``relevant=True``  → candidate stays at ``status='fetched'`` (ready for Stage 6).
    * ``relevant=False`` → candidate set to ``status='filtered'``; reason logged.
    * LLM error          → candidate set to ``status='review'`` (human fallback).

    The LLM call is synchronous (ordering matters for audit consistency).
    Each call is logged to ``audit_log`` via ``LLMClient``.

Prompt
------
    Template: ``src/newsfeed/prompts/relevance_v1.j2``
    Version:  ``relevance_v1``

Run directly
------------
    uv run python -m newsfeed.stage5_relevance
"""

from __future__ import annotations

import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from newsfeed.db import Candidate, ContextBundle, get_session
from newsfeed.llm_client import LLMClient, LLMStructuredOutputError

# ---------------------------------------------------------------------------
# LLM response schema
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_VERSION = "relevance_v1"
_MAX_TEXT_CHARS = 3_000  # truncate article text before sending to LLM
_SYSTEM_PROMPT = (
    "You are an expert analyst for an Australian water sector regulatory newsfeed. "
    "Determine whether each article is relevant for publication. "
    "Be conservative: only mark an article as relevant if it clearly concerns "
    "water regulation, policy, legislation, or management in Australia."
)


class RelevanceResult(BaseModel):
    """LLM response for the binary relevance gate."""

    model_config = ConfigDict(extra="ignore")

    relevant: bool
    reason: str


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(
    article_text: str,
    section: str,
    jurisdiction: str,
    source_url: str | None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("relevance_v1.j2")
    return template.render(
        article_text=article_text[:_MAX_TEXT_CHARS],
        section=section,
        jurisdiction=jurisdiction,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Per-candidate processing
# ---------------------------------------------------------------------------


def _check_one(
    candidate: Candidate,
    bundle: ContextBundle,
    llm: LLMClient,
    run_id: str,
) -> str:
    """Run the relevance gate for one candidate. Returns the new status string."""
    article_text = bundle.main_text or ""
    if not article_text.strip():
        logger.warning(
            "empty bundle text — routing to review", candidate_id=candidate.id, url=candidate.url
        )
        return "review"

    prompt = _build_prompt(
        article_text=article_text,
        section=candidate.section_hint or "",
        jurisdiction=candidate.jurisdiction_hint or "",
        source_url=candidate.url,
    )

    try:
        result = llm.complete(
            prompt=prompt,
            schema=RelevanceResult,
            model_tier="cheap",
            system=_SYSTEM_PROMPT,
            stage="stage5_relevance",
            candidate_id=candidate.id,
            prompt_version=_PROMPT_VERSION,
            run_id=run_id,
        )
    except LLMStructuredOutputError as exc:
        logger.error(
            "LLM failed for relevance — routing to review",
            candidate_id=candidate.id,
            url=candidate.url,
            error=str(exc),
        )
        return "review"

    if result.relevant:
        logger.info(
            "relevant",
            candidate_id=candidate.id,
            url=candidate.url,
            reason=result.reason,
        )
        return "fetched"  # unchanged — Stage 6 picks up 'fetched' candidates

    logger.info(
        "not relevant — filtered",
        candidate_id=candidate.id,
        url=candidate.url,
        reason=result.reason,
    )
    return "filtered"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(run_id: str | None = None) -> dict[str, int]:
    """Run relevance filter over all ``status='fetched'`` candidates.

    Returns ``{"checked": N, "relevant": M, "filtered": K, "review": R}``.
    """
    run_id = run_id or uuid.uuid4().hex
    llm = LLMClient()

    with get_session() as session:
        rows: list[tuple[Candidate, ContextBundle]] = list(
            session.execute(
                select(Candidate, ContextBundle)
                .join(ContextBundle, Candidate.id == ContextBundle.candidate_id)
                .where(Candidate.status == "fetched")
            ).all()
        )

    if not rows:
        logger.info("relevance filter: no fetched candidates")
        return {"checked": 0, "relevant": 0, "filtered": 0, "review": 0}

    logger.info("relevance filter start", candidates=len(rows))

    relevant_count = 0
    filtered_count = 0
    review_count = 0

    for candidate, bundle in rows:
        new_status = _check_one(candidate, bundle, llm, run_id)

        if new_status != "fetched":  # only write DB when status actually changes
            with get_session() as session:
                cand = session.get(Candidate, candidate.id)
                if cand:
                    cand.status = new_status

        if new_status == "fetched":
            relevant_count += 1
        elif new_status == "filtered":
            filtered_count += 1
        else:
            review_count += 1

    counts = {
        "checked": len(rows),
        "relevant": relevant_count,
        "filtered": filtered_count,
        "review": review_count,
    }
    logger.info("relevance filter complete", **counts)
    return counts


if __name__ == "__main__":
    run()
