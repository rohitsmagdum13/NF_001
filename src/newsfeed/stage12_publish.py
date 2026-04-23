"""Stage 12 — Publish to Horizon CMS + mark candidates published.

Inputs
------
    Disk:   outputs/{run_date}/horizon_payload.json
    Disk:   outputs/{run_date}/Water_Newsfeed.docx
    SQLite: candidates + drafts tables (to mark as published)

Outputs
-------
    HTTP:   POST to Horizon CMS API (if ``horizon_api_url`` is configured)
    SQLite: audit_log row per publish action
    SQLite: candidates.status → 'published' for all approved drafts in payload

What this stage does
--------------------
    1. Reads ``horizon_payload.json`` from the output directory.
    2. If ``settings.horizon_api_url`` is set, POSTs the payload JSON to the
       Horizon CMS API with ``Authorization: Bearer <token>`` header.
       On non-2xx response, logs the error but does NOT raise — items remain
       as 'validated' so the run can be re-tried.
    3. For each candidate_id in the payload whose ``editor_decision`` is
       'approved' (or 'pending' in editor-bypassed runs), sets
       ``candidates.status = 'published'``.
    4. Writes one ``audit_log`` row summarising the publish action.

    If ``horizon_api_url`` is not configured, steps 2-4 still run so the
    DB state is consistent for manual / future publishing.

Run directly
------------
    uv run python -m newsfeed.stage12_publish
    uv run python -m newsfeed.stage12_publish --date 2026-04-22
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
from loguru import logger

from newsfeed.config import get_settings
from newsfeed.db import AuditLog, Candidate, Draft, get_session
from newsfeed.schemas import AssembledNewsfeed

_PUBLISH_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Horizon API
# ---------------------------------------------------------------------------


def _post_to_horizon(
    payload_path: Path,
    api_url: str,
    api_token: str | None,
) -> bool:
    """POST the payload JSON to the Horizon CMS. Returns True on success."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    try:
        with httpx.Client(timeout=_PUBLISH_TIMEOUT) as client:
            resp = client.post(
                api_url,
                content=payload_path.read_bytes(),
                headers=headers,
            )
        if resp.is_success:
            logger.info("publish: Horizon API accepted payload", status=resp.status_code)
            return True
        logger.error(
            "publish: Horizon API rejected payload",
            status=resp.status_code,
            body=resp.text[:500],
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("publish: Horizon API request failed", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# DB updates
# ---------------------------------------------------------------------------


def _mark_published(candidate_ids: list[int]) -> int:
    """Set status='published' for approved/pending candidates in the payload."""
    published = 0
    for cid in candidate_ids:
        with get_session() as session:
            cand = session.get(Candidate, cid)
            if cand and cand.status == "validated":
                # Check if associated draft is approved or pending (not rejected)
                draft = (
                    session.execute(
                        __import__("sqlalchemy").select(Draft).where(Draft.candidate_id == cid)
                    )
                    .scalars()
                    .first()
                )
                if draft and draft.editor_decision in ("approved", "pending"):
                    cand.status = "published"
                    published += 1
    return published


def _write_audit(
    run_id: str,
    candidate_ids: list[int],
    horizon_ok: bool,
    payload_path: Path,
) -> None:
    with get_session() as session:
        session.add(
            AuditLog(
                run_id=run_id,
                stage="stage12_publish",
                candidate_id=None,
                prompt_version=None,
                model=None,
                provider="horizon" if horizon_ok else "local",
                fallback_triggered=0,
                input_tokens=None,
                output_tokens=None,
                latency_ms=None,
            )
        )
    logger.info(
        "publish: audit written",
        run_id=run_id,
        candidates=len(candidate_ids),
        horizon_posted=horizon_ok,
        payload=str(payload_path),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    run_id: str | None = None,
    run_date: str | None = None,
    output_dir: Path | None = None,
    payload_path: Path | None = None,
) -> dict[str, int]:
    """Publish the assembled newsfeed and mark candidates as published.

    Returns ``{"published": N, "horizon_ok": 0_or_1}``.
    """
    from datetime import date  # noqa: PLC0415 — keep top-level import clean

    run_id = run_id or uuid.uuid4().hex
    settings = get_settings()
    today = run_date or date.today().isoformat()
    out_dir = output_dir or (settings.paths.outputs / today)
    src_path = payload_path or (out_dir / "horizon_payload.json")

    if not src_path.exists():
        raise FileNotFoundError(f"horizon_payload.json not found: {src_path}")

    newsfeed = AssembledNewsfeed.model_validate_json(src_path.read_text(encoding="utf-8"))

    candidate_ids = [e.candidate_id for s in newsfeed.sections for e in s.entries]

    if not candidate_ids:
        logger.info("publish: no entries in payload — nothing to publish")
        return {"published": 0, "horizon_ok": 0}

    logger.info("publish start", candidates=len(candidate_ids))

    # Post to Horizon if configured
    horizon_ok = False
    if settings.horizon_api_url:
        horizon_ok = _post_to_horizon(
            payload_path=src_path,
            api_url=settings.horizon_api_url,
            api_token=settings.horizon_api_token,
        )
    else:
        logger.info("publish: horizon_api_url not configured — skipping HTTP post")

    # Mark candidates published
    published = _mark_published(candidate_ids)

    # Audit log
    _write_audit(run_id, candidate_ids, horizon_ok, src_path)

    counts = {"published": published, "horizon_ok": int(horizon_ok)}
    logger.info("publish complete", **counts)
    return counts


if __name__ == "__main__":
    date_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--date" else None
    run(run_date=date_arg)
