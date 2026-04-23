"""List all Anthropic models available in your AWS Bedrock account.

Reads credentials from .env (via project settings), then calls the
Bedrock API to list foundation models and cross-region inference profiles,
filtering to Anthropic only.

Usage
-----
    python scripts/list_bedrock_models.py
    python scripts/list_bedrock_models.py --region us-east-1
    python scripts/list_bedrock_models.py --all          # include non-Anthropic models
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is on the path so we can load settings
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newsfeed.config import get_settings


def _make_client(region: str, settings):
    import boto3
    return boto3.client(
        "bedrock",
        region_name=region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        aws_session_token=settings.aws_session_token or None,
    )


def list_foundation_models(client, anthropic_only: bool) -> list[dict]:
    """Return foundation models from Bedrock."""
    resp = client.list_foundation_models(byProvider="Anthropic" if anthropic_only else "")
    models = resp.get("modelSummaries", [])
    return sorted(models, key=lambda m: m.get("modelId", ""))


def list_inference_profiles(client, anthropic_only: bool) -> list[dict]:
    """Return cross-region inference profiles (where Claude 4.x lives)."""
    try:
        resp = client.list_inference_profiles(typeEquals="SYSTEM_DEFINED")
        profiles = resp.get("inferenceProfileSummaries", [])
        if anthropic_only:
            profiles = [p for p in profiles if "anthropic" in p.get("inferenceProfileId", "").lower()]
        return sorted(profiles, key=lambda p: p.get("inferenceProfileId", ""))
    except Exception as exc:
        print(f"  [inference profiles not available in this region: {exc}]")
        return []


def print_table(rows: list[dict], id_key: str, name_key: str, extra_keys: list[str]) -> None:
    if not rows:
        print("  (none found)")
        return
    col_w = 65
    header = f"  {'Model ID':<{col_w}}  {'Name':<40}"
    print(header)
    print("  " + "-" * (col_w + 42))
    for row in rows:
        mid = row.get(id_key, "")
        name = row.get(name_key, "")
        extras = "  ".join(f"{k}={row.get(k,'')}" for k in extra_keys if row.get(k))
        suffix = f"  [{extras}]" if extras else ""
        print(f"  {mid:<{col_w}}  {name:<40}{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description="List Anthropic models in AWS Bedrock")
    parser.add_argument("--region", default=None, help="AWS region (default: from config.yaml)")
    parser.add_argument("--all", action="store_true", dest="all_providers",
                        help="Show all providers, not just Anthropic")
    args = parser.parse_args()

    settings = get_settings()
    region = args.region or settings.bedrock.region
    anthropic_only = not args.all_providers

    print(f"\nBedrock model lookup")
    print(f"  Region  : {region}")
    print(f"  Filter  : {'Anthropic only' if anthropic_only else 'all providers'}")
    print(f"  AWS key : {'set' if settings.aws_access_key_id else 'NOT SET'}")
    print()

    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        print("ERROR: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set in .env")
        sys.exit(1)

    try:
        client = _make_client(region, settings)
    except Exception as exc:
        print(f"ERROR creating boto3 client: {exc}")
        sys.exit(1)

    # ── Foundation models ──────────────────────────────────────────────────
    print("── Foundation models ──────────────────────────────────────────")
    try:
        fm = list_foundation_models(client, anthropic_only)
        print_table(fm, "modelId", "modelName",
                    ["responseStreamingSupported", "inferenceTypesSupported"])
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print()

    # ── Cross-region inference profiles (Claude 4.x) ──────────────────────
    print("── Cross-region inference profiles (Claude 4.x lives here) ───")
    try:
        profiles = list_inference_profiles(client, anthropic_only)
        print_table(profiles, "inferenceProfileId", "inferenceProfileName", ["status"])
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print()
    print("── Currently configured in config.yaml ────────────────────────")
    print(f"  region        : {settings.bedrock.region}")
    print(f"  cheap_model   : {settings.bedrock.cheap_model}")
    print(f"  quality_model : {settings.bedrock.quality_model}")
    print()
    print("Copy the Model ID you want and paste it into config.yaml under bedrock:")
    print("  bedrock:")
    print("    region: <region>")
    print("    cheap_model: <model-id>")
    print("    quality_model: <model-id>")
    print()


if __name__ == "__main__":
    main()
