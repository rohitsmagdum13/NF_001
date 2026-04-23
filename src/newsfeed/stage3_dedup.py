"""Stage 3 — Deduplication and seen-hash TTL pruning.

Inputs
------
    SQLite: candidates table    (status='new', added by Stages 1 and 2)
    SQLite: seen_hashes table

Outputs
-------
    SQLite: seen_hashes table   (entries older than TTL deleted)

What this stage does
--------------------
    1. Counts ``candidates`` rows still in ``status='new'`` — these are ready
       for Stage 4 (fetch).
    2. Prunes ``seen_hashes`` entries older than ``lookback_days × 4`` days.
       Without pruning the dedup table grows unbounded, and articles that ran
       more than 4 lookback windows ago would never be re-discovered even if
       they become relevant again.

Notes
-----
    Item-level dedup (blocking the same article from being inserted twice in one
    run) is already enforced at insertion time in Stages 1 / 2 via the
    ``seen_hashes`` check in ``_persist_batch``. The ``candidates.hash`` UNIQUE
    constraint provides a second safety net. Stage 3 therefore focuses on the
    TTL maintenance pass.

Run directly
------------
    uv run python -m newsfeed.stage3_dedup
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, func, select

from newsfeed.config import get_settings
from newsfeed.db import Candidate, SeenHash, get_session


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run() -> dict[str, int]:
    """Prune stale seen-hash entries and report candidate counts.

    Returns a counts dict::

        {
            "candidates_new": N,   # ready for Stage 4
            "pruned_hashes": M,    # removed from seen_hashes
            "ttl_days": T,         # effective TTL (lookback_days × 4)
        }
    """
    settings = get_settings()
    ttl_days = settings.pipeline.lookback_days * 4
    cutoff = _utcnow() - timedelta(days=ttl_days)

    with get_session() as session:
        candidates_new: int = (
            session.execute(
                select(func.count()).select_from(Candidate).where(Candidate.status == "new")
            ).scalar()
            or 0
        )

        result = session.execute(delete(SeenHash).where(SeenHash.first_seen_at < cutoff))
        pruned: int = result.rowcount

    counts: dict[str, int] = {
        "candidates_new": candidates_new,
        "pruned_hashes": pruned,
        "ttl_days": ttl_days,
    }

    if pruned:
        logger.info("pruned stale seen-hashes", **counts)
    else:
        logger.info("dedup complete — no stale hashes to prune", **counts)

    return counts


if __name__ == "__main__":
    run()
