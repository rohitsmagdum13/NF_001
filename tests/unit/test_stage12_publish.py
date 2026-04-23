"""Unit tests for Stage 12 — publish to Horizon + mark published."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from newsfeed.db import AuditLog, Candidate, ContextBundle, Draft, get_session
from newsfeed.schemas import AssembledEntry, AssembledNewsfeed, AssembledSection
from newsfeed.stage12_publish import _mark_published, _post_to_horizon, run


def _write_payload(tmp_path: Path, candidate_ids: list[int]) -> Path:
    entries = [
        AssembledEntry(
            candidate_id=cid,
            draft_id=cid,
            section="Water Reform",
            jurisdiction="Victoria",
            content_type="media_release",
            draft_text="**Test Entry**\n*(Source: DEECA)*",
        )
        for cid in candidate_ids
    ]
    newsfeed = AssembledNewsfeed(
        run_date="2024-04-22",
        sections=[AssembledSection(section="Water Reform", entries=entries)],
    )
    payload_path = tmp_path / "horizon_payload.json"
    payload_path.write_text(newsfeed.model_dump_json(), encoding="utf-8")
    return payload_path


def _add_validated_candidate(cid_hint: str = "a") -> tuple[int, int]:
    """Insert a validated candidate + approved draft. Returns (candidate_id, draft_id)."""
    with get_session() as session:
        c = Candidate(
            source_id=f"src__{cid_hint}",
            source_type="url",
            url=f"https://example.com/pub-{cid_hint}",
            title="Publish Test",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint="Water Reform",
            jurisdiction_hint="Victoria",
            hash=f"hash_pub_{cid_hint}",
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
            section="Water Reform",
            jurisdiction="Victoria",
            confidence=0.9,
            draft_text="**Title**\n*(Source: DEECA)*",
            editor_decision="approved",
        )
        session.add(d)
        session.flush()
        return cid, int(d.id)


# ---------------------------------------------------------------------------
# _post_to_horizon
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_post_to_horizon_returns_true_on_success(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text('{"run_date": "2024-04-22"}', encoding="utf-8")

    with patch("newsfeed.stage12_publish.httpx.Client") as MockClient:
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        MockClient.return_value.__enter__.return_value.post.return_value = mock_resp

        result = _post_to_horizon(payload_path, "https://horizon.example.com/api", "token123")

    assert result is True


@pytest.mark.unit
def test_post_to_horizon_returns_false_on_error_status(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text('{"run_date": "2024-04-22"}', encoding="utf-8")

    with patch("newsfeed.stage12_publish.httpx.Client") as MockClient:
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        MockClient.return_value.__enter__.return_value.post.return_value = mock_resp

        result = _post_to_horizon(payload_path, "https://horizon.example.com/api", None)

    assert result is False


@pytest.mark.unit
def test_post_to_horizon_returns_false_on_network_error(tmp_path: Path) -> None:
    import httpx

    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{}", encoding="utf-8")

    with patch("newsfeed.stage12_publish.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError(
            "refused"
        )

        result = _post_to_horizon(payload_path, "https://horizon.example.com/api", None)

    assert result is False


# ---------------------------------------------------------------------------
# _mark_published
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_published_sets_status_to_published(tmp_db_path: Path) -> None:
    cid, _ = _add_validated_candidate("b")
    published = _mark_published([cid])
    assert published == 1

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "published"


@pytest.mark.unit
def test_mark_published_skips_missing_candidate(tmp_db_path: Path) -> None:
    published = _mark_published([9999])
    assert published == 0


@pytest.mark.unit
def test_mark_published_skips_rejected_draft(tmp_db_path: Path) -> None:
    with get_session() as session:
        c = Candidate(
            source_id="src__rej",
            source_type="url",
            url="https://example.com/rejected",
            title="Rejected",
            pub_date=datetime(2024, 4, 15, tzinfo=UTC),
            section_hint="Water Reform",
            jurisdiction_hint="Victoria",
            hash="hash_pub_rej",
            status="validated",
        )
        session.add(c)
        session.flush()
        cid = int(c.id)
        session.add(
            ContextBundle(candidate_id=cid, main_text=".", sublinks_json="[]", metadata_json="{}")
        )
        d = Draft(
            candidate_id=cid,
            content_type="media_release",
            section="Water Reform",
            jurisdiction="Victoria",
            confidence=0.9,
            draft_text="**Title**",
            editor_decision="rejected",
        )
        session.add(d)

    published = _mark_published([cid])
    assert published == 0

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "validated"  # unchanged


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_marks_candidates_published(tmp_db_path: Path, tmp_path: Path) -> None:
    cid, _ = _add_validated_candidate("c")
    payload_path = _write_payload(tmp_path, [cid])

    counts = run(run_id="test-pub", output_dir=tmp_path, payload_path=payload_path)

    assert counts["published"] == 1

    with get_session() as session:
        cand = session.get(Candidate, cid)
    assert cand is not None
    assert cand.status == "published"


@pytest.mark.unit
def test_run_skips_horizon_post_when_not_configured(tmp_db_path: Path, tmp_path: Path) -> None:
    cid, _ = _add_validated_candidate("d")
    payload_path = _write_payload(tmp_path, [cid])

    with patch("newsfeed.stage12_publish._post_to_horizon") as mock_post:
        counts = run(run_id="test-no-horizon", output_dir=tmp_path, payload_path=payload_path)

    mock_post.assert_not_called()
    assert counts["horizon_ok"] == 0


@pytest.mark.unit
def test_run_raises_on_missing_payload(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run(run_id="test-missing", output_dir=tmp_path, payload_path=tmp_path / "nope.json")


@pytest.mark.unit
def test_run_returns_zeros_for_empty_payload(tmp_db_path: Path, tmp_path: Path) -> None:
    newsfeed = AssembledNewsfeed(run_date="2024-04-22", sections=[])
    payload_path = tmp_path / "horizon_payload.json"
    payload_path.write_text(newsfeed.model_dump_json(), encoding="utf-8")

    counts = run(run_id="test-empty", output_dir=tmp_path, payload_path=payload_path)
    assert counts == {"published": 0, "horizon_ok": 0}


@pytest.mark.unit
def test_run_writes_audit_log_entry(tmp_db_path: Path, tmp_path: Path) -> None:
    cid, _ = _add_validated_candidate("e")
    payload_path = _write_payload(tmp_path, [cid])

    run(run_id="test-audit-pub", output_dir=tmp_path, payload_path=payload_path)

    with get_session() as session:
        from sqlalchemy import select

        audit = (
            session.execute(select(AuditLog).where(AuditLog.run_id == "test-audit-pub"))
            .scalars()
            .first()
        )

    assert audit is not None
    assert audit.stage == "stage12_publish"
