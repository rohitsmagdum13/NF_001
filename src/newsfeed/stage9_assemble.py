"""Stage 9 — Assemble validated drafts into a structured newsfeed.

Inputs
------
    SQLite: candidates table    (status='validated')
    SQLite: drafts table        (draft_text, section, jurisdiction, editor_decision)

Outputs
-------
    Disk:   outputs/{run_date}/horizon_payload.json  (AssembledNewsfeed JSON)
    Return: {"assembled": N, "sections": M, "skipped": K}

What this stage does
--------------------
    Collects all ``status='validated'`` candidates whose draft has
    ``editor_decision IN ('pending', 'approved')`` (not rejected).

    Groups entries by section (fixed order from config) then jurisdiction
    (fixed order: National first, then alphabetical-by-config). Empty
    section/jurisdiction combos are omitted.

    Writes the result to ``outputs/{run_date}/horizon_payload.json`` as an
    ``AssembledNewsfeed`` JSON object. Stage 11 (render) reads this file to
    produce the final .docx.

Run directly
------------
    uv run python -m newsfeed.stage9_assemble
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, Draft, get_session
from newsfeed.schemas import AssembledEntry, AssembledNewsfeed, AssembledSection

_EDITOR_DECISIONS_INCLUDE = frozenset({"pending", "approved"})


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------


def _section_rank(section: str, section_order: list[str]) -> int:
    try:
        return section_order.index(section)
    except ValueError:
        return len(section_order)


def _jurisdiction_rank(jurisdiction: str, jurisdiction_order: list[str]) -> int:
    try:
        return jurisdiction_order.index(jurisdiction)
    except ValueError:
        return len(jurisdiction_order)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    run_id: str | None = None,
    run_date: str | None = None,
    output_dir: Path | None = None,
    include_published: bool = False,
) -> dict[str, int]:
    """Assemble validated drafts and write horizon_payload.json.

    Args:
        include_published: Also include candidates with status='published'.
            Useful when re-assembling after Stage 12 has already run.

    Returns ``{"assembled": N, "sections": M, "skipped": K}``.
    """
    run_id = run_id or uuid.uuid4().hex
    settings = get_settings()
    today = run_date or date.today().isoformat()
    out_dir = output_dir or (settings.paths.outputs / today)

    statuses = ["validated", "published"] if include_published else ["validated"]

    with get_session() as session:
        rows: list[tuple[Candidate, Draft]] = list(
            session.execute(
                select(Candidate, Draft)
                .join(Draft, Candidate.id == Draft.candidate_id)
                .where(Candidate.status.in_(statuses))
                .where(Draft.draft_text.is_not(None))
            ).all()
        )

    if not rows:
        logger.info("assemble: no validated candidates")
        return {"assembled": 0, "sections": 0, "skipped": 0}

    logger.info("assemble start", candidates=len(rows))

    section_order = settings.sections
    jurisdiction_order = settings.jurisdictions

    assembled: list[AssembledEntry] = []
    skipped = 0

    for candidate, draft in rows:
        if draft.editor_decision not in _EDITOR_DECISIONS_INCLUDE:
            skipped += 1
            continue
        if not draft.draft_text or not draft.section or not draft.jurisdiction:
            skipped += 1
            continue

        assembled.append(
            AssembledEntry(
                candidate_id=int(candidate.id),
                draft_id=int(draft.id),
                section=draft.section,
                jurisdiction=draft.jurisdiction,
                content_type=draft.content_type or "simple_update",
                draft_text=draft.draft_text,
                source_url=candidate.url,
                source_label=candidate.source_label,
            )
        )

    assembled.sort(
        key=lambda e: (
            _section_rank(e.section, section_order),
            _jurisdiction_rank(e.jurisdiction, jurisdiction_order),
        )
    )

    # Group by section (preserving sorted order)
    section_map: dict[str, list[AssembledEntry]] = {}
    for entry in assembled:
        section_map.setdefault(entry.section, []).append(entry)

    sections = [
        AssembledSection(section=sec, entries=entries)
        for sec, entries in sorted(
            section_map.items(),
            key=lambda kv: _section_rank(kv[0], section_order),
        )
    ]

    newsfeed = AssembledNewsfeed(
        run_date=today,
        sections=sections,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    payload_path = out_dir / "horizon_payload.json"
    payload_path.write_text(
        newsfeed.model_dump_json(indent=2),
        encoding="utf-8",
    )

    counts = {
        "assembled": len(assembled),
        "sections": len(sections),
        "skipped": skipped,
    }
    logger.info(
        "assemble complete",
        payload=str(payload_path),
        **counts,
    )
    return counts


if __name__ == "__main__":
    run()
