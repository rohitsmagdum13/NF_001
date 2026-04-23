"""Unit tests for Stage 5 — LLM relevance filter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from newsfeed.db import Candidate, ContextBundle, get_session
from newsfeed.llm_client import LLMStructuredOutputError
from newsfeed.stage5_relevance import (
    RelevanceResult,
    _build_prompt,
    _check_one,
    run,
)


def _add_fetched_candidate(
    url: str = "https://example.com/water-policy",
    section: str = "Water Reform",
    jurisdiction: str = "Victoria",
    text: str = "The Victorian government announced new water reform legislation today.",
) -> int:
    with get_session() as session:
        c = Candidate(
            source_id="water_reform__vic__abc123",
            source_type="url",
            url=url,
            title="Water Policy Update",
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
# _build_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_prompt_contains_article_text() -> None:
    prompt = _build_prompt(
        article_text="The Victorian Water Authority announced new reforms.",
        section="Water Reform",
        jurisdiction="Victoria",
        source_url="https://example.com/article",
    )
    assert "Victorian Water Authority" in prompt
    assert "Water Reform" in prompt
    assert "Victoria" in prompt


@pytest.mark.unit
def test_build_prompt_truncates_long_text() -> None:
    long_text = "x" * 10_000
    prompt = _build_prompt(
        article_text=long_text,
        section="Water Reform",
        jurisdiction="National",
        source_url=None,
    )
    # Truncated text appears, but not 10000 chars of it
    assert "x" * 3_000 in prompt
    assert "x" * 3_001 not in prompt


@pytest.mark.unit
def test_build_prompt_handles_none_source_url() -> None:
    prompt = _build_prompt(
        article_text="Some article text here.",
        section="Conservation",
        jurisdiction="Queensland",
        source_url=None,
    )
    assert "Conservation" in prompt
    assert "Queensland" in prompt


# ---------------------------------------------------------------------------
# _check_one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_one_returns_fetched_when_relevant(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()
    llm.complete.return_value = RelevanceResult(relevant=True, reason="Clearly about water reform.")

    status = _check_one(candidate, bundle, llm, run_id="test-run")

    assert status == "fetched"
    llm.complete.assert_called_once()


@pytest.mark.unit
def test_check_one_returns_filtered_when_not_relevant(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()
    llm.complete.return_value = RelevanceResult(relevant=False, reason="Unrelated job listing.")

    status = _check_one(candidate, bundle, llm, run_id="test-run")

    assert status == "filtered"


@pytest.mark.unit
def test_check_one_returns_review_on_llm_error(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()
    llm.complete.side_effect = LLMStructuredOutputError("Both providers failed")

    status = _check_one(candidate, bundle, llm, run_id="test-run")

    assert status == "review"


@pytest.mark.unit
def test_check_one_returns_review_for_empty_bundle_text(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(text="")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        assert candidate is not None

    llm = MagicMock()

    status = _check_one(candidate, bundle, llm, run_id="test-run")

    assert status == "review"
    llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_filters_irrelevant_candidates(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/irrelevant")

    with patch("newsfeed.stage5_relevance.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.return_value = RelevanceResult(
            relevant=False, reason="Not about water regulation."
        )
        counts = run(run_id="test-run-filter")

    assert counts["filtered"] == 1
    assert counts["relevant"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "filtered"


@pytest.mark.unit
def test_run_keeps_relevant_candidates_as_fetched(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/relevant")

    with patch("newsfeed.stage5_relevance.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.return_value = RelevanceResult(
            relevant=True, reason="Directly about water reform legislation."
        )
        counts = run(run_id="test-run-keep")

    assert counts["relevant"] == 1
    assert counts["filtered"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "fetched"  # unchanged — Stage 6 will classify it


@pytest.mark.unit
def test_run_routes_llm_errors_to_review(tmp_db_path: Path) -> None:
    cid = _add_fetched_candidate(url="https://example.com/llm-error")

    with patch("newsfeed.stage5_relevance.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.side_effect = LLMStructuredOutputError("Both providers failed")
        counts = run(run_id="test-run-error")

    assert counts["review"] == 1

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "review"


@pytest.mark.unit
def test_run_returns_zeros_with_no_fetched_candidates(tmp_db_path: Path) -> None:
    counts = run(run_id="test-run-empty")
    assert counts == {"checked": 0, "relevant": 0, "filtered": 0, "review": 0}


@pytest.mark.unit
def test_run_processes_multiple_candidates(tmp_db_path: Path) -> None:
    _add_fetched_candidate(url="https://example.com/art1")
    _add_fetched_candidate(url="https://example.com/art2")
    _add_fetched_candidate(url="https://example.com/art3")

    responses = [
        RelevanceResult(relevant=True, reason="Relevant."),
        RelevanceResult(relevant=False, reason="Not relevant."),
        RelevanceResult(relevant=True, reason="Relevant."),
    ]

    with patch("newsfeed.stage5_relevance.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.side_effect = responses
        counts = run(run_id="test-run-multi")

    assert counts["checked"] == 3
    assert counts["relevant"] == 2
    assert counts["filtered"] == 1
    assert counts["review"] == 0
