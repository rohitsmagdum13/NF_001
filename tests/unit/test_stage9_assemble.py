"""Unit tests for Stage 9 — newsfeed assembly."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsfeed.db import Candidate, ContextBundle, Draft, get_session
from newsfeed.schemas import AssembledNewsfeed
from newsfeed.stage9_assemble import _jurisdiction_rank, _section_rank, run


def _add_validated_candidate(
    url: str,
    section: str = "Water Reform",
    jurisdiction: str = "Victoria",
    draft_text: str = "**Water Reform Update**\nDEECA announced reforms.\n*(Source: DEECA)*",
    editor_decision: str = "pending",
) -> tuple[int, int]:
    """Returns (candidate_id, draft_id)."""
    with get_session() as session:
        c = Candidate(
            source_id=f"src__{url[-8:]}",
            source_type="url",
            url=url,
            title="Test Candidate",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint=section,
            jurisdiction_hint=jurisdiction,
            hash=f"hash_{url}",
            status="validated",
        )
        session.add(c)
        session.flush()
        cid = int(c.id)
        session.add(
            ContextBundle(
                candidate_id=cid,
                main_text="Source text.",
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
            editor_decision=editor_decision,
        )
        session.add(d)
        session.flush()
        return cid, int(d.id)


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_section_rank_known_section() -> None:
    order = ["Water Reform", "Conservation", "Water Rights"]
    assert _section_rank("Conservation", order) == 1


@pytest.mark.unit
def test_section_rank_unknown_falls_to_end() -> None:
    order = ["Water Reform", "Conservation"]
    assert _section_rank("Unknown Section", order) == 2


@pytest.mark.unit
def test_jurisdiction_rank_national_first() -> None:
    order = ["National", "Victoria", "New South Wales"]
    assert _jurisdiction_rank("National", order) == 0
    assert _jurisdiction_rank("Victoria", order) == 1


@pytest.mark.unit
def test_jurisdiction_rank_unknown_falls_to_end() -> None:
    order = ["National", "Victoria"]
    assert _jurisdiction_rank("Atlantis", order) == 2


# ---------------------------------------------------------------------------
# run() — basic assembly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_returns_zeros_with_no_validated_candidates(tmp_db_path: Path, tmp_path: Path) -> None:
    counts = run(run_id="test-empty", run_date="2024-04-15", output_dir=tmp_path)
    assert counts == {"assembled": 0, "sections": 0, "skipped": 0}


@pytest.mark.unit
def test_run_assembles_single_candidate(tmp_db_path: Path, tmp_path: Path) -> None:
    cid, did = _add_validated_candidate(url="https://example.com/a1")
    counts = run(run_id="test-single", run_date="2024-04-15", output_dir=tmp_path)

    assert counts["assembled"] == 1
    assert counts["sections"] == 1
    assert counts["skipped"] == 0


@pytest.mark.unit
def test_run_writes_horizon_payload_json(tmp_db_path: Path, tmp_path: Path) -> None:
    _add_validated_candidate(url="https://example.com/payload-test")
    run(run_id="test-payload", run_date="2024-04-15", output_dir=tmp_path)

    payload_path = tmp_path / "horizon_payload.json"
    assert payload_path.exists()

    newsfeed = AssembledNewsfeed.model_validate_json(payload_path.read_text(encoding="utf-8"))
    assert newsfeed.run_date == "2024-04-15"
    assert len(newsfeed.sections) == 1
    assert newsfeed.sections[0].section == "Water Reform"


@pytest.mark.unit
def test_run_groups_entries_by_section(tmp_db_path: Path, tmp_path: Path) -> None:
    _add_validated_candidate(url="https://example.com/s1", section="Water Reform")
    _add_validated_candidate(url="https://example.com/s2", section="Conservation")
    _add_validated_candidate(url="https://example.com/s3", section="Water Reform")

    counts = run(run_id="test-group", run_date="2024-04-15", output_dir=tmp_path)

    assert counts["assembled"] == 3
    assert counts["sections"] == 2

    payload_path = tmp_path / "horizon_payload.json"
    newsfeed = AssembledNewsfeed.model_validate_json(payload_path.read_text(encoding="utf-8"))

    section_names = [s.section for s in newsfeed.sections]
    assert "Water Reform" in section_names
    assert "Conservation" in section_names

    reform_section = next(s for s in newsfeed.sections if s.section == "Water Reform")
    assert len(reform_section.entries) == 2


@pytest.mark.unit
def test_run_orders_sections_by_config_order(tmp_db_path: Path, tmp_path: Path) -> None:
    # Water Rights (index 6) should come after Water Reform (index 0)
    _add_validated_candidate(url="https://example.com/o1", section="Water Rights")
    _add_validated_candidate(url="https://example.com/o2", section="Water Reform")

    run(run_id="test-order", run_date="2024-04-15", output_dir=tmp_path)

    payload_path = tmp_path / "horizon_payload.json"
    newsfeed = AssembledNewsfeed.model_validate_json(payload_path.read_text(encoding="utf-8"))

    section_names = [s.section for s in newsfeed.sections]
    assert section_names.index("Water Reform") < section_names.index("Water Rights")


@pytest.mark.unit
def test_run_orders_entries_by_jurisdiction(tmp_db_path: Path, tmp_path: Path) -> None:
    # National (0) should come before Victoria (1)
    _add_validated_candidate(
        url="https://example.com/j1", section="Conservation", jurisdiction="Victoria"
    )
    _add_validated_candidate(
        url="https://example.com/j2", section="Conservation", jurisdiction="National"
    )

    run(run_id="test-juris", run_date="2024-04-15", output_dir=tmp_path)

    payload_path = tmp_path / "horizon_payload.json"
    newsfeed = AssembledNewsfeed.model_validate_json(payload_path.read_text(encoding="utf-8"))

    section = newsfeed.sections[0]
    jurisdictions = [e.jurisdiction for e in section.entries]
    assert jurisdictions.index("National") < jurisdictions.index("Victoria")


@pytest.mark.unit
def test_run_skips_rejected_drafts(tmp_db_path: Path, tmp_path: Path) -> None:
    _add_validated_candidate(url="https://example.com/r1", editor_decision="rejected")
    _add_validated_candidate(url="https://example.com/r2", editor_decision="approved")

    counts = run(run_id="test-reject", run_date="2024-04-15", output_dir=tmp_path)

    assert counts["assembled"] == 1
    assert counts["skipped"] == 1


@pytest.mark.unit
def test_run_includes_approved_and_pending_drafts(tmp_db_path: Path, tmp_path: Path) -> None:
    _add_validated_candidate(url="https://example.com/inc1", editor_decision="approved")
    _add_validated_candidate(url="https://example.com/inc2", editor_decision="pending")

    counts = run(run_id="test-include", run_date="2024-04-15", output_dir=tmp_path)

    assert counts["assembled"] == 2
    assert counts["skipped"] == 0


@pytest.mark.unit
def test_run_payload_contains_copyright_footer(tmp_db_path: Path, tmp_path: Path) -> None:
    _add_validated_candidate(url="https://example.com/footer")
    run(run_id="test-footer", run_date="2024-04-15", output_dir=tmp_path)

    payload_path = tmp_path / "horizon_payload.json"
    newsfeed = AssembledNewsfeed.model_validate_json(payload_path.read_text(encoding="utf-8"))
    assert newsfeed.copyright_footer != ""
