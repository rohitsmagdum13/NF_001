"""Pipeline entrypoint. CLI + Prefect flow.

Stage 0 (parse-template) is wired. Later stages are plugged in incrementally.

Usage
-----
    uv run python main.py run --lookback-days 7
    uv run python main.py parse-template
    uv run python main.py discover --limit 3
    uv run python main.py watch-inbox
    uv run python main.py render --date 2026-04-22
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from loguru import logger
from prefect import flow, task

from newsfeed import (
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
    stage12_publish,
)
from newsfeed.config import get_settings
from newsfeed.db import init_db


@task
def _stage_stub(name: str) -> str:
    logger.info("stage stub invoked", stage=name)
    return name


@task(name="stage0-parse-template")
def _stage0_task() -> dict[str, int]:
    return stage0_parse_template.run()


@task(name="stage1-discovery")
def _stage1_task(limit: int | None = None) -> dict[str, int]:
    return stage1_discovery.run(limit=limit)


@task(name="stage3-dedup")
def _stage3_task() -> dict[str, int]:
    return stage3_dedup.run()


@task(name="stage4-fetch")
def _stage4_task() -> dict[str, int]:
    return stage4_fetch.run()


@task(name="stage5-relevance")
def _stage5_task(run_id: str | None = None) -> dict[str, int]:
    return stage5_relevance.run(run_id=run_id)


@task(name="stage6-classify")
def _stage6_task(run_id: str | None = None) -> dict[str, int]:
    return stage6_classify.run(run_id=run_id)


@task(name="stage7-draft")
def _stage7_task(run_id: str | None = None) -> dict[str, int]:
    return stage7_draft.run(run_id=run_id)


@task(name="stage8-validate")
def _stage8_task(run_id: str | None = None) -> dict[str, int]:
    return stage8_validate.run(run_id=run_id)


@task(name="stage9-assemble")
def _stage9_task(run_id: str | None = None) -> dict[str, int]:
    return stage9_assemble.run(run_id=run_id)


@task(name="stage11-render")
def _stage11_task(run_date: str | None = None) -> dict[str, int]:
    return stage11_render.run(run_date=run_date)


@task(name="stage12-publish")
def _stage12_task(run_id: str | None = None, run_date: str | None = None) -> dict[str, int]:
    return stage12_publish.run(run_id=run_id, run_date=run_date)


@flow(name="newsfeed-pipeline")
def pipeline_flow(lookback_days: int | None = None) -> None:
    """End-to-end pipeline flow. Stages are plugged in incrementally."""
    settings = get_settings()
    days = lookback_days or settings.pipeline.lookback_days
    logger.info("pipeline start", lookback_days=days)

    # Stage 0: parse template → source registry (blocking — downstream need registry on disk).
    _stage0_task()

    # Stage 1: discover candidates from source registry.
    _stage1_task()

    # Stage 3: TTL-prune seen_hashes (Stage 2 PDF ingestion runs in parallel via watch-inbox).
    _stage3_task()

    # Stage 4: fetch full article content and build context bundles.
    _stage4_task()

    # Stage 5: LLM relevance gate — irrelevant candidates set to 'filtered'.
    _stage5_task()

    # Stage 6: LLM classification — section + jurisdiction + content_type.
    _stage6_task()

    # Stage 7: LLM draft generation — fill template slots from context bundle.
    _stage7_task()

    # Stage 8: Hallucination guard — quoted phrases, dates, schema checks.
    _stage8_task()

    # Stage 9: Assemble validated drafts into ordered horizon_payload.json.
    _stage9_task()

    # Stage 11: Render validated drafts to .docx and .md.
    _stage11_task(run_date=date.today().isoformat())

    # Stage 12: Publish to Horizon CMS and mark candidates published.
    _stage12_task()

    logger.info("pipeline end")


def _configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level, enqueue=False)


def _cmd_run(args: argparse.Namespace) -> int:
    init_db()
    pipeline_flow(lookback_days=args.lookback_days)
    return 0


def _cmd_parse_template(_: argparse.Namespace) -> int:
    init_db()
    counts = stage0_parse_template.run()
    logger.info("parse-template complete", **counts)
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    init_db()
    counts = stage1_discovery.run(limit=args.limit)
    logger.info("discover complete", **counts)
    return 0


def _cmd_watch_inbox(_: argparse.Namespace) -> int:
    init_db()
    logger.info("watch-inbox stub — Stage 2 not wired yet")
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    init_db()
    counts = stage9_assemble.run(
        run_date=args.date or date.today().isoformat(),
        include_published=args.include_published,
    )
    logger.info("assemble complete", **counts)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    init_db()
    target = args.date or date.today().isoformat()
    counts = stage11_render.run(run_date=target)
    logger.info("render complete", **counts)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsfeed", description="Regulatory newsfeed pipeline")
    subs = parser.add_subparsers(dest="command", required=True)

    run_p = subs.add_parser("run", help="Run the full pipeline once")
    run_p.add_argument("--lookback-days", type=int, default=None)
    run_p.set_defaults(func=_cmd_run)

    pt_p = subs.add_parser(
        "parse-template", help="Stage 0 — parse template → source_registry.json"
    )
    pt_p.set_defaults(func=_cmd_parse_template)

    disc_p = subs.add_parser("discover", help="Run Stage 1 discovery only")
    disc_p.add_argument("--limit", type=int, default=None)
    disc_p.add_argument("--source-id", type=str, default=None)
    disc_p.set_defaults(func=_cmd_discover)

    watch_p = subs.add_parser("watch-inbox", help="Watch ./inbox for PDFs (Stage 2)")
    watch_p.set_defaults(func=_cmd_watch_inbox)

    assemble_p = subs.add_parser("assemble", help="Stage 9 — assemble drafts into horizon_payload.json")
    assemble_p.add_argument("--date", type=str, default=None)
    assemble_p.add_argument(
        "--include-published",
        action="store_true",
        default=False,
        help="Also include already-published candidates (use when re-running after Stage 12)",
    )
    assemble_p.set_defaults(func=_cmd_assemble)

    render_p = subs.add_parser("render", help="Render approved drafts to .docx")
    render_p.add_argument("--date", type=str, default=None)
    render_p.set_defaults(func=_cmd_render)

    return parser


def main() -> int:
    _configure_logging()
    parser = _build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
