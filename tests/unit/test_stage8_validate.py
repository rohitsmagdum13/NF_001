"""Unit tests for Stage 8 — hallucination guard + schema validation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.stage8_validate import (
    _check_dates,
    _check_jurisdiction,
    _check_non_empty,
    _check_quoted_phrases,
    _check_section,
    run,
    validate_draft,
)


def _add_drafted_candidate(
    url: str = "https://example.com/validate-test",
    section: str = "Water Reform",
    jurisdiction: str = "Victoria",
    source_text: str = 'DEECA announced reforms on 15 April 2024. The minister stated "water security is paramount".',
    draft_text: str = '**Water Reform**\nDEECA announced that "[water security is paramount]".\nDEECA\'s media release (15 April 2024)\n*(Source: DEECA)*',
) -> tuple[int, int]:
    """Returns (candidate_id, draft_id)."""
    with get_session() as session:
        c = Candidate(
            source_id="water_reform__vic__val1",
            source_type="url",
            url=url,
            title="Validation Test",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint=section,
            jurisdiction_hint=jurisdiction,
            hash=f"hash_{url}",
            status="drafted",
        )
        session.add(c)
        session.flush()
        cid = int(c.id)
        session.add(
            ContextBundle(
                candidate_id=cid,
                main_text=source_text,
                sublinks_json="[]",
                metadata_json="{}",
            )
        )
        d = Draft(
            candidate_id=cid,
            content_type="media_release",
            section=section,
            jurisdiction=jurisdiction,
            confidence=0.9,
            draft_text=draft_text,
            editor_decision="pending",
        )
        session.add(d)
        session.flush()
        return cid, int(d.id)


# ---------------------------------------------------------------------------
# _check_non_empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_non_empty_passes_for_content() -> None:
    assert _check_non_empty("Some content here") == []


@pytest.mark.unit
def test_check_non_empty_fails_for_empty_string() -> None:
    failures = _check_non_empty("")
    assert len(failures) == 1
    assert failures[0].check == "non_empty"


@pytest.mark.unit
def test_check_non_empty_fails_for_whitespace_only() -> None:
    failures = _check_non_empty("   \n\t  ")
    assert len(failures) == 1
    assert failures[0].check == "non_empty"


# ---------------------------------------------------------------------------
# _check_quoted_phrases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_quoted_phrases_passes_when_phrase_in_source() -> None:
    draft = 'DEECA announced that "[water security is paramount]".'
    source = "The minister stated water security is paramount in the announcement."
    failures = _check_quoted_phrases(draft, source)
    assert failures == []


@pytest.mark.unit
def test_check_quoted_phrases_fails_when_phrase_not_in_source() -> None:
    draft = 'DEECA announced that "[invented phrase not in source]".'
    source = "DEECA made an announcement about water."
    failures = _check_quoted_phrases(draft, source)
    assert len(failures) == 1
    assert failures[0].check == "quoted_phrase"
    assert "invented phrase not in source" in failures[0].detail


@pytest.mark.unit
def test_check_quoted_phrases_handles_multiple_quotes() -> None:
    draft = 'Stated "[first quote]" and also "[second quote]".'
    source = "first quote and second quote both appear here"
    failures = _check_quoted_phrases(draft, source)
    assert failures == []


@pytest.mark.unit
def test_check_quoted_phrases_reports_each_missing_phrase() -> None:
    draft = 'Stated "[missing one]" and "[missing two]".'
    source = "Source has neither"
    failures = _check_quoted_phrases(draft, source)
    assert len(failures) == 2


@pytest.mark.unit
def test_check_quoted_phrases_passes_when_no_quotes_in_draft() -> None:
    draft = "DEECA made an announcement about water reform."
    source = "Some source text."
    failures = _check_quoted_phrases(draft, source)
    assert failures == []


# ---------------------------------------------------------------------------
# _check_dates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_dates_passes_for_date_in_source() -> None:
    draft = "The plan commenced on 15 April 2024."
    source = "The water plan, dated 15 April 2024, came into effect."
    failures = _check_dates(draft, source)
    assert failures == []


@pytest.mark.unit
def test_check_dates_fails_for_date_not_in_source() -> None:
    draft = "The plan commenced on 15 April 2024."
    source = "The water plan came into effect in 2023."
    failures = _check_dates(draft, source)
    assert len(failures) == 1
    assert failures[0].check == "date_reference"


@pytest.mark.unit
def test_check_dates_handles_month_year_format() -> None:
    draft = "Updated in April 2024 by the agency."
    source = "The agency updated the plan in April 2024."
    failures = _check_dates(draft, source)
    assert failures == []


@pytest.mark.unit
def test_check_dates_passes_when_no_dates_in_draft() -> None:
    draft = "DEECA has made available an update."
    source = "Some source without dates."
    failures = _check_dates(draft, source)
    assert failures == []


# ---------------------------------------------------------------------------
# _check_section
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_section_passes_for_valid_section() -> None:
    assert _check_section("Water Reform") == []


@pytest.mark.unit
def test_check_section_fails_for_invalid_section() -> None:
    failures = _check_section("Not A Real Section")
    assert len(failures) == 1
    assert failures[0].check == "section"


@pytest.mark.unit
def test_check_section_fails_for_empty_section() -> None:
    failures = _check_section("")
    assert len(failures) == 1
    assert failures[0].check == "section"


@pytest.mark.unit
def test_check_section_fails_for_none_section() -> None:
    failures = _check_section(None)
    assert len(failures) == 1
    assert failures[0].check == "section"


# ---------------------------------------------------------------------------
# _check_jurisdiction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_jurisdiction_passes_for_valid_jurisdiction() -> None:
    assert _check_jurisdiction("Victoria") == []


@pytest.mark.unit
def test_check_jurisdiction_fails_for_invalid_jurisdiction() -> None:
    failures = _check_jurisdiction("Atlantis")
    assert len(failures) == 1
    assert failures[0].check == "jurisdiction"


@pytest.mark.unit
def test_check_jurisdiction_fails_for_none_jurisdiction() -> None:
    failures = _check_jurisdiction(None)
    assert len(failures) == 1
    assert failures[0].check == "jurisdiction"


# ---------------------------------------------------------------------------
# validate_draft (integration of all checks)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_draft_passes_clean_draft() -> None:
    draft_text = '**Water Reform**\nDEECA announced that "[water security is paramount]".\nDEECA\'s release (15 April 2024)\n*(Source: DEECA)*'
    source_text = "DEECA announced reforms. water security is paramount. Dated 15 April 2024."
    failures = validate_draft(
        draft_text=draft_text,
        source_text=source_text,
        section="Water Reform",
        jurisdiction="Victoria",
    )
    assert failures == []


@pytest.mark.unit
def test_validate_draft_accumulates_multiple_failures() -> None:
    draft_text = 'Draft with "[hallucinated quote]" dated 1 January 2099.'
    source_text = "DEECA made an announcement about water."
    failures = validate_draft(
        draft_text=draft_text,
        source_text=source_text,
        section="Fake Section",
        jurisdiction="Atlantis",
    )
    checks = {f.check for f in failures}
    assert "quoted_phrase" in checks
    assert "date_reference" in checks
    assert "section" in checks
    assert "jurisdiction" in checks


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_validates_clean_draft(tmp_db_path: Path) -> None:
    cid, did = _add_drafted_candidate(url="https://example.com/val-clean")
    counts = run(run_id="test-val-clean")

    assert counts["validated"] == 1
    assert counts["review"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
        draft = session.get(Draft, did)

    assert cand is not None
    assert cand.status == "validated"
    assert draft is not None
    assert draft.validation_errors_json == "[]"


@pytest.mark.unit
def test_run_routes_failed_draft_to_review(tmp_db_path: Path) -> None:
    cid, did = _add_drafted_candidate(
        url="https://example.com/val-fail",
        source_text="DEECA made an announcement.",
        draft_text='**Reform**\nDEECA said "[invented quote not in source]".\n*(Source: DEECA)*',
    )
    counts = run(run_id="test-val-fail")

    assert counts["review"] == 1
    assert counts["validated"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
        draft = session.get(Draft, did)

    assert cand is not None
    assert cand.status == "review"
    assert draft is not None
    errors = __import__("json").loads(draft.validation_errors_json or "[]")
    assert any(e["check"] == "quoted_phrase" for e in errors)


@pytest.mark.unit
def test_run_returns_zeros_with_no_drafted_candidates(tmp_db_path: Path) -> None:
    counts = run(run_id="test-val-empty")
    assert counts == {"checked": 0, "validated": 0, "review": 0}


@pytest.mark.unit
def test_run_handles_draft_with_invalid_section(tmp_db_path: Path) -> None:
    cid, did = _add_drafted_candidate(
        url="https://example.com/val-section",
        section="Not A Section",
        draft_text="**Update**\nDEECA made an announcement.\n*(Source: DEECA)*",
        source_text="DEECA made an announcement.",
    )
    # Patch the candidate's section via Draft
    with get_session() as session:
        d = session.get(Draft, did)
        if d:
            d.section = "Not A Section"

    counts = run(run_id="test-val-section")

    assert counts["review"] == 1

    with get_session() as session:
        draft = session.get(Draft, did)

    assert draft is not None
    errors = __import__("json").loads(draft.validation_errors_json or "[]")
    assert any(e["check"] == "section" for e in errors)
