"""Stage 8 — Hallucination guard + schema validation (pure Python, no LLM).

Inputs
------
    SQLite: candidates table    (status='drafted')
    SQLite: drafts table        (draft_text, section, jurisdiction, content_type)
    SQLite: context_bundles     (main_text as source truth)

Outputs
-------
    SQLite: drafts table        (validation_errors_json updated)
    SQLite: candidates table    (status → 'validated' or 'review')

Checks performed
----------------
    1. Non-empty draft    — draft_text must contain visible characters.
    2. Quoted phrases     — every ``"[...]"`` phrase must appear verbatim in source.
    3. Date references    — every date string in draft_text must appear in source.
    4. Section validity   — draft.section ∈ configured sections list.
    5. Jurisdiction check — draft.jurisdiction ∈ configured jurisdictions list.

    All failing checks are accumulated; a draft with ANY failure goes to
    ``status='review'`` with the errors stored as JSON for the editor.
    Drafts that pass all checks → ``status='validated'``.

Run directly
------------
    uv run python -m newsfeed.stage8_validate
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, ContextBundle, Draft, get_session

if TYPE_CHECKING:
    pass

# Date patterns to search for in draft text.
_DATE_PATTERNS = [
    re.compile(
        r"\b\d{1,2}\s+"
        r"(?:January|February|March|April|May|June|July|August"
        r"|September|October|November|December)\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August"
        r"|September|October|November|December)\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
]

_QUOTE_PATTERN = re.compile(r'"\[([^\]]+)\]"')


@dataclass
class ValidationFailure:
    check: str
    detail: str


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _check_non_empty(draft_text: str) -> list[ValidationFailure]:
    if not draft_text.strip():
        return [ValidationFailure(check="non_empty", detail="draft_text is empty or whitespace")]
    return []


def _check_quoted_phrases(draft_text: str, source_text: str) -> list[ValidationFailure]:
    """Every "[quoted phrase]" in draft must appear verbatim in source."""
    failures: list[ValidationFailure] = []
    for match in _QUOTE_PATTERN.finditer(draft_text):
        phrase = match.group(1)
        if phrase not in source_text:
            failures.append(
                ValidationFailure(
                    check="quoted_phrase",
                    detail=f"Quoted phrase not found in source: {phrase!r}",
                )
            )
    return failures


def _check_dates(draft_text: str, source_text: str) -> list[ValidationFailure]:
    """Every date-like string in the draft must appear verbatim in source."""
    all_dates: set[str] = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(draft_text):
            all_dates.add(match.group(0))

    # Drop shorter dates that are substrings of a longer date already collected
    # (e.g. "April 2024" is a substring of "15 April 2024" — report only the longer).
    deduped = {d for d in all_dates if not any(d != other and d in other for other in all_dates)}

    failures: list[ValidationFailure] = []
    for date_str in sorted(deduped):
        if date_str not in source_text:
            failures.append(
                ValidationFailure(
                    check="date_reference",
                    detail=f"Date not found in source: {date_str!r}",
                )
            )
    return failures


def _check_section(section: str | None) -> list[ValidationFailure]:
    settings = get_settings()
    if not section:
        return [ValidationFailure(check="section", detail="section is empty")]
    if settings.sections and section not in settings.sections:
        return [
            ValidationFailure(
                check="section",
                detail=f"Unknown section: {section!r}",
            )
        ]
    return []


def _check_jurisdiction(jurisdiction: str | None) -> list[ValidationFailure]:
    settings = get_settings()
    if not jurisdiction:
        return [ValidationFailure(check="jurisdiction", detail="jurisdiction is empty")]
    if settings.jurisdictions and jurisdiction not in settings.jurisdictions:
        return [
            ValidationFailure(
                check="jurisdiction",
                detail=f"Unknown jurisdiction: {jurisdiction!r}",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Per-draft validation
# ---------------------------------------------------------------------------


def validate_draft(
    draft_text: str,
    source_text: str,
    section: str | None,
    jurisdiction: str | None,
) -> list[ValidationFailure]:
    """Run all checks. Returns accumulated failures (empty = all passed)."""
    failures: list[ValidationFailure] = []
    failures.extend(_check_non_empty(draft_text))
    if not failures:
        # Only run further checks if draft is non-empty
        failures.extend(_check_quoted_phrases(draft_text, source_text))
        failures.extend(_check_dates(draft_text, source_text))
    failures.extend(_check_section(section))
    failures.extend(_check_jurisdiction(jurisdiction))
    return failures


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(run_id: str | None = None) -> dict[str, int]:
    """Validate all ``status='drafted'`` candidates.

    Returns ``{"checked": N, "validated": M, "review": R}``.
    """
    run_id = run_id or uuid.uuid4().hex

    with get_session() as session:
        rows: list[tuple[Candidate, ContextBundle, Draft]] = list(
            session.execute(
                select(Candidate, ContextBundle, Draft)
                .join(ContextBundle, Candidate.id == ContextBundle.candidate_id)
                .join(Draft, Candidate.id == Draft.candidate_id)
                .where(Candidate.status == "drafted")
                .where(Draft.draft_text.is_not(None))
            ).all()
        )

    if not rows:
        logger.info("validate: no drafted candidates")
        return {"checked": 0, "validated": 0, "review": 0}

    logger.info("validate start", candidates=len(rows))

    validated_count = 0
    review_count = 0

    for candidate, bundle, draft in rows:
        source_text = bundle.main_text or ""
        draft_text = draft.draft_text or ""

        failures = validate_draft(
            draft_text=draft_text,
            source_text=source_text,
            section=draft.section,
            jurisdiction=draft.jurisdiction,
        )

        errors_json = json.dumps([{"check": f.check, "detail": f.detail} for f in failures])

        if failures:
            new_status = "review"
            review_count += 1
            logger.info(
                "validate: failed — routing to review",
                candidate_id=candidate.id,
                failures=[f.check for f in failures],
            )
        else:
            new_status = "validated"
            validated_count += 1
            logger.info("validate: passed", candidate_id=candidate.id)

        with get_session() as session:
            d = session.get(Draft, draft.id)
            if d:
                d.validation_errors_json = errors_json
            cand = session.get(Candidate, candidate.id)
            if cand:
                cand.status = new_status

    counts = {"checked": len(rows), "validated": validated_count, "review": review_count}
    logger.info("validate complete", **counts)
    return counts


if __name__ == "__main__":
    run()
