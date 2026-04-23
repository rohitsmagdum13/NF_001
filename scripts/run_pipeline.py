"""Regulatory Newsfeed Pipeline — full end-to-end runner.

Runs every stage in sequence with structured logging, per-stage timing,
exception isolation, and a final summary table.

Usage
-----
    uv run python scripts/run_pipeline.py
    uv run python scripts/run_pipeline.py --lookback-days 14
    uv run python scripts/run_pipeline.py --date 2026-04-23
    uv run python scripts/run_pipeline.py --from-stage 5
    uv run python scripts/run_pipeline.py --stop-after 9
    uv run python scripts/run_pipeline.py --from-stage 9 --include-published

Exit codes
----------
    0 — all critical stages passed
    1 — one or more critical stages failed
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

# ── project root on sys.path so imports work whether called from root or scripts/
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from loguru import logger  # noqa: E402

from newsfeed import (  # noqa: E402
    stage0_parse_template,
    stage1_discovery,
    stage3_dedup,
    stage4_fetch,
    stage5_relevance,
    stage6_classify,
    stage7_draft,
    stage8_validate,
    stage9_assemble,
    stage11_render,
)
from newsfeed.config import get_settings  # noqa: E402
from newsfeed.db import init_db  # noqa: E402

# ── colour codes (works on any terminal that supports ANSI) ──────────────────
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


# ── stage result ─────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    number: int
    name: str
    status: str = "pending"   # pending | ok | failed | skipped
    counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    error: str | None = None
    critical: bool = True     # if False, failure is warned but pipeline continues

    def mark_ok(self, counts: dict[str, int], elapsed: float) -> None:
        self.status = "ok"
        self.counts = counts
        self.elapsed_s = elapsed

    def mark_failed(self, exc: BaseException, elapsed: float) -> None:
        self.status = "failed"
        self.elapsed_s = elapsed
        self.error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    def mark_skipped(self) -> None:
        self.status = "skipped"

    @property
    def status_icon(self) -> str:
        return {
            "ok":      f"{_GREEN}✓ OK     {_RESET}",
            "failed":  f"{_RED}✗ FAILED {_RESET}",
            "skipped": f"{_YELLOW}⊘ SKIPPED{_RESET}",
            "pending": f"{_CYAN}… PENDING{_RESET}",
        }[self.status]

    @property
    def counts_str(self) -> str:
        if not self.counts:
            return "—"
        return "  ".join(f"{k}={v}" for k, v in self.counts.items())


# ── helpers ───────────────────────────────────────────────────────────────────

def _banner(label: str) -> None:
    width = 72
    bar   = "─" * width
    logger.info(f"\n{_BOLD}{bar}\n  {label}\n{bar}{_RESET}")


def _run_stage(
    result: StageResult,
    fn: Callable[[], dict[str, int]],
) -> bool:
    """Call *fn*, populate *result*, return True on success."""
    _banner(f"Stage {result.number} — {result.name}")
    t0 = time.monotonic()
    try:
        counts = fn()
        elapsed = time.monotonic() - t0
        result.mark_ok(counts, elapsed)
        logger.info(
            f"Stage {result.number} complete",
            status="ok",
            elapsed_s=f"{elapsed:.1f}s",
            **counts,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        result.mark_failed(exc, elapsed)
        logger.error(
            f"Stage {result.number} FAILED",
            stage=result.name,
            elapsed_s=f"{elapsed:.1f}s",
            error=str(exc),
        )
        logger.debug("Traceback:\n" + result.error)
        return False


def _print_summary(results: list[StageResult], total_s: float) -> None:
    """Print the final summary table to stderr so it always shows."""
    lines = [
        "",
        f"{_BOLD}{'─'*72}{_RESET}",
        f"{_BOLD}  PIPELINE SUMMARY{_RESET}",
        f"{'─'*72}",
        f"  {'Stage':<6} {'Name':<35} {'Status':<18} {'Time':>6}  Counts",
        f"  {'─'*6} {'─'*35} {'─'*18} {'─'*6}  {'─'*20}",
    ]
    for r in results:
        lines.append(
            f"  {r.number:<6} {r.name:<35} {r.status_icon:<18} "
            f"{r.elapsed_s:>5.1f}s  {r.counts_str}"
        )
    lines += [
        f"{'─'*72}",
        f"  Total elapsed: {total_s:.1f}s",
        f"{'─'*72}",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)

    failed = [r for r in results if r.status == "failed"]
    if failed:
        print(f"{_RED}{_BOLD}FAILED stages:{_RESET}", file=sys.stderr)
        for r in failed:
            print(f"  Stage {r.number} — {r.name}", file=sys.stderr)
            # Print first 5 lines of traceback
            for line in (r.error or "").splitlines()[:8]:
                print(f"    {line}", file=sys.stderr)
        print("", file=sys.stderr)


# ── stage definitions ─────────────────────────────────────────────────────────

def _build_stages(args: argparse.Namespace) -> list[tuple[StageResult, Callable]]:
    """Return (result, fn) pairs for every pipeline stage."""
    run_date = args.date or date.today().isoformat()

    return [
        (
            StageResult(0, "Parse Template → source_registry.json", critical=False),
            lambda: stage0_parse_template.run(),
        ),
        (
            StageResult(1, "Discovery → candidates"),
            lambda: stage1_discovery.run(),
        ),
        (
            StageResult(3, "Dedup → prune seen_hashes"),
            lambda: stage3_dedup.run(),
        ),
        (
            StageResult(4, "Fetch → context bundles"),
            lambda: stage4_fetch.run(),
        ),
        (
            StageResult(5, "Relevance filter (LLM)"),
            lambda: stage5_relevance.run(),
        ),
        (
            StageResult(6, "Classify section/jurisdiction (LLM)"),
            lambda: stage6_classify.run(),
        ),
        (
            StageResult(7, "Draft generation (LLM)"),
            lambda: stage7_draft.run(),
        ),
        (
            StageResult(8, "Hallucination guard + validation"),
            lambda: stage8_validate.run(),
        ),
        (
            StageResult(9, "Assemble → horizon_payload.json"),
            lambda: stage9_assemble.run(
                run_date=run_date,
                include_published=args.include_published,
            ),
        ),
        (
            StageResult(11, "Render → .docx + .md"),
            lambda: stage11_render.run(run_date=run_date),
        ),
    ]


# ── main ──────────────────────────────────────────────────────────────────────

def _configure_logging(log_level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
        enqueue=False,
    )
    # Also write full DEBUG log to file for post-mortem inspection
    log_path = _ROOT / "outputs" / "pipeline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_path),
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}",
        enqueue=False,
    )
    logger.info("Log file: {}", log_path)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_pipeline",
        description="Run the regulatory newsfeed pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Override run date (default: today)",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        metavar="N",
        help="Override lookback window (default: from config.yaml)",
    )
    p.add_argument(
        "--from-stage",
        type=int,
        default=0,
        metavar="N",
        help="Start from stage N (skips earlier stages, default: 0)",
    )
    p.add_argument(
        "--stop-after",
        type=int,
        default=99,
        metavar="N",
        help="Stop after stage N (default: run all)",
    )
    p.add_argument(
        "--include-published",
        action="store_true",
        default=False,
        help="Stage 9: also include already-published candidates",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (default: INFO)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _configure_logging(args.log_level)

    # Override lookback if requested
    if args.lookback_days:
        import os
        os.environ["PIPELINE__LOOKBACK_DAYS"] = str(args.lookback_days)
        get_settings.cache_clear()

    settings = get_settings()
    run_date = args.date or date.today().isoformat()

    _banner(
        f"REGULATORY NEWSFEED PIPELINE  |  date={run_date}"
        f"  |  lookback={settings.pipeline.lookback_days}d"
    )
    logger.info("Initialising database …")
    init_db()

    stages = _build_stages(args)
    results: list[StageResult] = []
    pipeline_start = time.monotonic()
    failed_critical = False

    for result, fn in stages:
        # Skip stages outside the requested range
        if result.number < args.from_stage:
            result.mark_skipped()
            results.append(result)
            logger.debug(f"Stage {result.number} skipped (--from-stage={args.from_stage})")
            continue
        if result.number > args.stop_after:
            result.mark_skipped()
            results.append(result)
            logger.debug(f"Stage {result.number} skipped (--stop-after={args.stop_after})")
            continue

        # Abort remaining stages if a previous critical stage failed
        if failed_critical:
            result.mark_skipped()
            results.append(result)
            logger.warning(f"Stage {result.number} skipped — earlier stage failed")
            continue

        ok = _run_stage(result, fn)
        results.append(result)

        if not ok:
            if result.critical:
                failed_critical = True
            else:
                logger.warning(
                    f"Stage {result.number} failed but is non-critical — continuing",
                    stage=result.name,
                )

    total_s = time.monotonic() - pipeline_start
    _print_summary(results, total_s)

    return 1 if failed_critical else 0


if __name__ == "__main__":
    sys.exit(main())
