"""Unit tests for Stage 7 — LLM draft generation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.llm_client import LLMStructuredOutputError
from newsfeed.stage7_draft import (
    DraftResult,
    _build_prompt,
    _build_sublinks_text,
    _draft_one,
    _load_format_template,
    run,
)


def _add_classified_candidate(
    url: str = "https://example.com/draft-test",
    section: str = "Water Reform",
    jurisdiction: str = "Victoria",
    text: str = 'The DEECA announced new water plan reforms. The minister stated "[water security is paramount]".',
    content_type: str = "media_release",
) -> tuple[int, int]:
    """Returns (candidate_id, draft_id)."""
    with get_session() as session:
        c = Candidate(
            source_id="water_reform__vic__draft1",
            source_type="url",
            url=url,
            title="Draft Test",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint=section,
            jurisdiction_hint=jurisdiction,
            hash=f"hash_{url}",
            status="classified",
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
        d = Draft(
            candidate_id=cid,
            content_type=content_type,
            section=section,
            jurisdiction=jurisdiction,
            confidence=0.9,
            editor_decision="pending",
        )
        session.add(d)
        session.flush()
        did = int(d.id)
        return cid, did


# ---------------------------------------------------------------------------
# _load_format_template
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_format_template_returns_string_for_media_release() -> None:
    template = _load_format_template("media_release")
    assert isinstance(template, str)
    assert len(template) > 10
    assert "{TITLE}" in template or "{AGENCY}" in template


@pytest.mark.unit
def test_load_format_template_returns_string_for_all_types() -> None:
    for content_type in [
        "new_plan",
        "media_release",
        "weekly_report",
        "bullet_list",
        "simple_update",
        "gazette_bundle",
    ]:
        template = _load_format_template(content_type)
        assert isinstance(template, str)
        assert len(template) > 10


@pytest.mark.unit
def test_load_format_template_raises_for_unknown_type() -> None:
    with pytest.raises(FileNotFoundError):
        _load_format_template("nonexistent_type")


# ---------------------------------------------------------------------------
# _build_sublinks_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_sublinks_text_returns_empty_for_none() -> None:
    assert _build_sublinks_text(None) == ""


@pytest.mark.unit
def test_build_sublinks_text_returns_empty_for_empty_json() -> None:
    assert _build_sublinks_text("[]") == ""


@pytest.mark.unit
def test_build_sublinks_text_formats_sublinks() -> None:
    import json

    sublinks = [{"url": "https://example.com/doc.pdf", "text": "Water plan document"}]
    result = _build_sublinks_text(json.dumps(sublinks))
    assert "Water plan document" in result
    assert "https://example.com/doc.pdf" in result


@pytest.mark.unit
def test_build_sublinks_text_handles_invalid_json() -> None:
    result = _build_sublinks_text("not-json")
    assert result == ""


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_prompt_contains_article_text() -> None:
    prompt = _build_prompt(
        article_text="DEECA announced water reforms.",
        section="Water Reform",
        jurisdiction="Victoria",
        content_type="media_release",
        format_template="**{TITLE}**\n{AGENCY}",
        sublinks_text="",
    )
    assert "DEECA announced water reforms." in prompt
    assert "Water Reform" in prompt
    assert "Victoria" in prompt
    assert "media_release" in prompt


@pytest.mark.unit
def test_build_prompt_embeds_format_template() -> None:
    template = '**{TITLE}**\n{AGENCY} announced that "[{QUOTE}]".'
    prompt = _build_prompt(
        article_text="Some article.",
        section="Conservation",
        jurisdiction="National",
        content_type="media_release",
        format_template=template,
        sublinks_text="",
    )
    assert "{TITLE}" in prompt
    assert "{AGENCY}" in prompt


@pytest.mark.unit
def test_build_prompt_includes_sublinks_when_present() -> None:
    prompt = _build_prompt(
        article_text="Article text.",
        section="Water Reform",
        jurisdiction="Victoria",
        content_type="simple_update",
        format_template="{TITLE}",
        sublinks_text="[https://example.com/doc.pdf]\nDocument content here.",
    )
    assert "Document content here." in prompt


@pytest.mark.unit
def test_build_prompt_omits_sublinks_section_when_empty() -> None:
    prompt = _build_prompt(
        article_text="Article text.",
        section="Water Reform",
        jurisdiction="Victoria",
        content_type="simple_update",
        format_template="{TITLE}",
        sublinks_text="",
    )
    assert "SUPPLEMENTARY CONTENT" not in prompt


# ---------------------------------------------------------------------------
# _draft_one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_draft_one_returns_text_on_success(tmp_db_path: Path) -> None:
    cid, did = _add_classified_candidate()
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        draft = session.get(Draft, did)
        assert candidate is not None and draft is not None

    llm = MagicMock()
    llm.complete.return_value = DraftResult(
        draft_text="**Water Reform Update**\nDEECA announced reforms."
    )

    text = _draft_one(candidate, bundle, draft, llm, run_id="test-run")

    assert text is not None
    assert "DEECA" in text
    llm.complete.assert_called_once()


@pytest.mark.unit
def test_draft_one_returns_none_on_llm_error(tmp_db_path: Path) -> None:
    cid, did = _add_classified_candidate(url="https://example.com/d2")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        draft = session.get(Draft, did)
        assert candidate is not None and draft is not None

    llm = MagicMock()
    llm.complete.side_effect = LLMStructuredOutputError("Both providers failed")

    text = _draft_one(candidate, bundle, draft, llm, run_id="test-run")

    assert text is None


@pytest.mark.unit
def test_draft_one_returns_none_for_empty_bundle(tmp_db_path: Path) -> None:
    cid, did = _add_classified_candidate(url="https://example.com/d3", text="")
    with get_session() as session:
        candidate = session.get(Candidate, cid)
        bundle = session.execute(
            select(ContextBundle).where(ContextBundle.candidate_id == cid)
        ).scalar_one()
        draft = session.get(Draft, did)
        assert candidate is not None and draft is not None

    llm = MagicMock()

    text = _draft_one(candidate, bundle, draft, llm, run_id="test-run")

    assert text is None
    llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_drafts_classified_candidate(tmp_db_path: Path) -> None:
    cid, did = _add_classified_candidate(url="https://example.com/run-draft")

    with patch("newsfeed.stage7_draft.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.return_value = DraftResult(
            draft_text='**Water Plan**\nDEECA announced that "[water security is paramount]".'
        )
        counts = run(run_id="test-draft-run")

    assert counts["drafted"] == 1
    assert counts["review"] == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
        draft = session.get(Draft, did)

    assert cand is not None
    assert cand.status == "drafted"
    assert draft is not None
    assert draft.draft_text is not None
    assert "Water Plan" in draft.draft_text


@pytest.mark.unit
def test_run_routes_llm_failure_to_review(tmp_db_path: Path) -> None:
    cid, _ = _add_classified_candidate(url="https://example.com/run-draft-fail")

    with patch("newsfeed.stage7_draft.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        mock_instance.complete.side_effect = LLMStructuredOutputError("Both failed")
        counts = run(run_id="test-draft-fail")

    assert counts["review"] == 1

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "review"


@pytest.mark.unit
def test_run_returns_zeros_with_no_classified_candidates(tmp_db_path: Path) -> None:
    counts = run(run_id="test-draft-empty")
    assert counts == {"checked": 0, "drafted": 0, "review": 0}


@pytest.mark.unit
def test_run_skips_candidates_with_existing_draft_text(tmp_db_path: Path) -> None:
    cid, did = _add_classified_candidate(url="https://example.com/existing-draft")
    # Pre-populate draft_text
    with get_session() as session:
        draft = session.get(Draft, did)
        if draft:
            draft.draft_text = "Already drafted content."

    with patch("newsfeed.stage7_draft.LLMClient") as MockLLM:
        mock_instance = MockLLM.return_value
        counts = run(run_id="test-skip-existing")

    assert counts["checked"] == 0
    mock_instance.complete.assert_not_called()
