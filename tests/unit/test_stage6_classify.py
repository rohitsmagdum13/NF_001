"""Unit tests for Stage 6 — LLM classification."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.llm_client import LLMStructuredOutputError
from newsfeed.stage6_classify import (
    ClassifyResult,
    _build_prompt,
    _classify_one,
    run,
)


def _add_fetched_candidate(
    url: str = "https://example.com/water-reform",
    section: str = "Water Reform",
    jurisdiction: str = "Victoria",
    text: str = "The Victorian government announced new water reform legislation today.",
) -> int:
    with get_session() as session:
        c = Candidate(
            source_id="water_reform__vic__abc123",
            source_type="url",
            url=url,
            title="Water Reform Update",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint=section,
            jurisdiction_hint=jurisdiction,
            hash=f"hash_{url}",
            status="fetched",
        )
        session.add(c)
        session.flush()
        cid = int(c.id)
        session.add(
            ContextBundle(
                candidate_id=cid,
                main_text=text,
                sublinks_json="[]",
                metadata_json="{}",
            )
        )
        return cid


# ---------------------------------------------------------------------------
# ClassifyResult validators
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_result_rejects_unknown_section() -> None:
    with pytest.raises(Exception):
        ClassifyResult(
            section="Not A Section",
            jurisdiction="Victoria",
            confidence=0.9,
            content_type="media_release",
        )


@pytest.mark.unit
def test_classify_result_rejects_unknown_jurisdiction() -> None:
    with pytest.raises(Exception):
        ClassifyResult(
            section="Water Reform",
            jurisdiction="Atlantis",
            confidence=0.9,
            content_type="media_release",
        )


@pytest.mark.unit
def test_classify_result_rejects_unknown_content_type() -> None:
    with pytest.raises(Exception):
        ClassifyResult(
            section="Water Reform",
            jurisdiction="Victoria",
            confidence=0.9,
            content_type="unknown_type",
        )


@pytest.mark.unit
def test_classify_result_accepts_valid_values() -> None:
    result = ClassifyResult(
        section="Water Reform",
        jurisdiction="Victoria",
        confidence=0.85,
        content_type="media_release",
    )
    assert result.section == "Water Reform"
    assert result.jurisdiction == "Victoria"
    assert result.content_type == "media_release"


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_prompt_contains_article_text() -> None:
    prompt = _build_prompt(
        article_text="Victorian Water Authority announced new reforms.",
        section_hint="Water Reform",
        jurisdiction_hint="Victoria",
        examples=[],
    )
    assert "Victorian Water Authority" in prompt
    assert "Water Reform" in prompt
    assert "Victoria" in prompt


@pytest.mark.unit
def test_build_prompt_contains_valid_sections() -> None:
    prompt = _build_prompt(
        article_text="Some text",
        section_hint="Water Reform",
        jurisdiction_hint="National",
        examples=[],
    )
    assert "Water Reform" in prompt
    assert "Conservation" in prompt
    assert "Water Rights" in prompt


@pytest.mark.unit
def test_build_prompt_truncates_long_text() -> None:
    long_text = "x" * 5_000
    prompt = _build_prompt(
        article_text=long_text,
        section_hint="Water Reform",
        jurisdiction_hint="National",
        examples=[],
    )
    assert "x" * 3_000 in prompt
    assert "x" * 3_001 not in prompt


# ---------------------------------------------------------------------------
# _classify_one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_one_returns_result_on_success(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()
    llm.complete.return_value = ClassifyResult(
        section="Water Reform",
        jurisdiction="Victoria",
        confidence=0.9,
        content_type="media_release",
    )

    result = _classify_one(candidate, bundle, [], llm, run_id="test-run")

    assert result is not None
    assert result.section == "Water Reform"
    assert result.confidence == 0.9
    llm.complete.assert_called_once()


@pytest.mark.unit
def test_classify_one_returns_none_on_llm_error(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()
    llm.complete.side_effect = LLMStructuredOutputError("Both providers failed")

    result = _classify_one(candidate, bundle, [], llm, run_id="test-run")

    assert result is None


@pytest.mark.unit
def test_classify_one_returns_none_for_empty_text(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(text="")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()

    result = _classify_one(candidate, bundle, [], llm, run_id="test-run")

    assert result is None
    llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_classifies_high_confidence_candidate(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/classify-high")

    with patch("newsfeed.stage6_classify.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.return_value = ClassifyResult(
            section="Water Reform",
            jurisdiction="Victoria",
            confidence=0.92,
            content_type="media_release",
        )
        counts = run(run_id="test-classify-high")

    assert counts["classified"] == 1
    assert counts["review"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
        draft = session.execute(select(Draft).where(Draft.candidate_id == cid)).scalar_one_or_none()

    assert cand is not None
    assert cand.status == "classified"
    assert draft is not None
    assert draft.section == "Water Reform"
    assert draft.jurisdiction == "Victoria"
    assert draft.content_type == "media_release"
    assert draft.editor_decision == "pending"


@pytest.mark.unit
def test_run_routes_low_confidence_to_review(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/classify-low")

    with patch("newsfeed.stage6_classify.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.return_value = ClassifyResult(
            section="Conservation",
            jurisdiction="National",
            confidence=0.5,  # below 0.7 threshold
            content_type="simple_update",
        )
        counts = run(run_id="test-classify-low")

    assert counts["review"] == 1
    assert counts["classified"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "review"


@pytest.mark.unit
def test_run_routes_llm_error_to_review(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/classify-error")

    with patch("newsfeed.stage6_classify.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.side_effect = LLMStructuredOutputError("Both failed")
        counts = run(run_id="test-classify-err")

    assert counts["review"] == 1

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "review"


@pytest.mark.unit
def test_run_returns_zeros_with_no_fetched_candidates(tmp_db_path: Path) -> None:
    counts = run(run_id="test-classify-empty")
    assert counts == {"checked": 0, "classified": 0, "review": 0}


@pytest.mark.unit
def test_run_processes_multiple_candidates(tmp_db_path: Path) -> None:
    _add_fetched_candidate(url="https://example.com/c1")
    _add_fetched_candidate(url="https://example.com/c2")
    _add_fetched_candidate(url="https://example.com/c3")

    responses = [
        ClassifyResult(
            section="Water Reform",
            jurisdiction="Victoria",
            confidence=0.95,
            content_type="media_release",
        ),
        ClassifyResult(
            section="Conservation",
            jurisdiction="National",
            confidence=0.4,
            content_type="simple_update",
        ),
        ClassifyResult(
            section="Water Rights",
            jurisdiction="New South Wales",
            confidence=0.88,
            content_type="new_plan",
        ),
    ]

    with patch("newsfeed.stage6_classify.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.side_effect = responses
        counts = run(run_id="test-classify-multi")

    assert counts["checked"] == 3
    assert counts["classified"] == 2  # confidence >= 0.7
    assert counts["review"] == 1  # confidence 0.4 < 0.7
