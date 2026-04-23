"""Regulatory Newsfeed Drafting Automation (POC)."""

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

__version__ = "0.1.0"

__all__ = [
    "stage0_parse_template",
    "stage1_discovery",
    "stage3_dedup",
    "stage4_fetch",
    "stage5_relevance",
    "stage6_classify",
    "stage7_draft",
    "stage8_validate",
    "stage9_assemble",
    "stage11_render",
    "stage12_publish",
]
