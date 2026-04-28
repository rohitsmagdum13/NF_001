"""Pydantic settings loader.

Single source of truth for runtime configuration. Merges three layers:

    1. `config.yaml` (committed, non-secret defaults)
    2. `.env` / process environment (secrets, deploy-specific values)
    3. Explicit overrides passed to ``load_settings()``

Stage modules MUST import ``get_settings()`` rather than reading
`config.yaml` or environment variables directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


# --- Sub-models (mirror config.yaml sections) ---


class PipelineConfig(BaseModel):
    lookback_days: int = Field(default=7, ge=1, le=365)
    max_sublink_depth: int = Field(default=1, ge=0, le=3)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    cache_retention_days: int = Field(default=28, ge=1)
    fetch_concurrency: int = Field(default=8, ge=1, le=64)
    http_timeout_seconds: int = Field(default=15, ge=1, le=120)
    content_nav_patterns: list[str] = Field(default_factory=list)
    # When True, Stage 1 keeps only URLs that look like individual articles
    # (date in path, multi-word slug, or 3+ path segments). Filters out
    # category landing pages, /about, /contact, pagination, etc.
    article_shape_filter: bool = True


class PathsConfig(BaseModel):
    inbox: Path
    cache: Path
    outputs: Path
    db: Path
    source_registry: Path
    reference: Path
    local_articles: Path = Field(default=Path("./data/local_articles"))

    @field_validator(
        "inbox",
        "cache",
        "outputs",
        "db",
        "source_registry",
        "reference",
        "local_articles",
        mode="before",
    )
    @classmethod
    def _resolve(cls, v: Any) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (_PROJECT_ROOT / p).resolve()


class LLMConfig(BaseModel):
    primary: Literal["bedrock", "openai"] = "bedrock"
    fallback: Literal["bedrock", "openai"] = "openai"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    max_retries: int = Field(default=2, ge=0, le=10)


class BedrockConfig(BaseModel):
    region: str = "ap-southeast-2"
    cheap_model: str
    quality_model: str


class OpenAIConfig(BaseModel):
    cheap_model: str
    quality_model: str


# --- Root settings ---


class Settings(BaseSettings):
    """Project-wide settings merged from YAML + env."""

    model_config = SettingsConfigDict(
        env_file=str(_DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Secrets / env-driven (names match .env.example)
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    bedrock_region: str = "ap-southeast-2"
    openai_api_key: str | None = None

    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"

    horizon_api_url: str | None = None
    horizon_api_token: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # YAML-driven (filled by load_settings)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    paths: PathsConfig = Field(
        default_factory=lambda: PathsConfig(
            inbox=Path("./inbox"),
            cache=Path("./data/cache"),
            outputs=Path("./outputs"),
            db=Path("./data/pipeline.db"),
            source_registry=Path("./data/source_registry.json"),
            reference=Path("./reference"),
            local_articles=Path("./data/local_articles"),
        )
    )
    llm: LLMConfig = Field(default_factory=LLMConfig)
    bedrock: BedrockConfig = Field(
        default_factory=lambda: BedrockConfig(
            cheap_model="anthropic.claude-3-5-haiku-20241022-v1:0",
            quality_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        )
    )
    openai: OpenAIConfig = Field(
        default_factory=lambda: OpenAIConfig(
            cheap_model="gpt-4o-mini",
            quality_model="gpt-4o",
        )
    )

    sections: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    agencies: dict[str, list[str]] = Field(default_factory=dict)
    content_types: list[str] = Field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at top of {path}, got {type(data).__name__}")
    return data


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings. Uses `config.yaml` at project root by default."""
    yaml_data = _load_yaml(config_path or _DEFAULT_CONFIG_PATH)
    return Settings(**yaml_data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Call ``get_settings.cache_clear()`` to reload."""
    return load_settings()
