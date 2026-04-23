"""SQLAlchemy models + session factory for the pipeline SQLite database.

Mirrors the schema documented in ``CLAUDE.md``. Stage modules communicate
via these tables; never by direct imports of each other.

Usage
-----
    from newsfeed.db import get_session, init_db

    init_db()            # idempotent — creates tables if missing
    with get_session() as session:
        session.add(Candidate(...))
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from newsfeed.config import get_settings

# Candidate lifecycle states. Keep in sync with Stage modules.
CANDIDATE_STATUSES: tuple[str, ...] = (
    "new",
    "fetched",
    "filtered",
    "classified",
    "drafted",
    "validated",
    "review",
    "approved",
    "rejected",
    "published",
)

SOURCE_TYPES: tuple[str, ...] = ("url", "html", "md", "pdf")


def _utcnow() -> datetime:
    """Timezone-aware UTC now — replaces deprecated ``datetime.utcnow()``."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Candidate(Base):
    """One discovered item (article / PDF entry / local file)."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(
        String,
        CheckConstraint(
            f"source_type IN ({', '.join(repr(s) for s in SOURCE_TYPES)})",
            name="ck_candidates_source_type",
        ),
        nullable=False,
    )
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    pub_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    section_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    jurisdiction_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    source_label: Mapped[str | None] = mapped_column(String, nullable=True)

    hash: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in CANDIDATE_STATUSES)})",
            name="ck_candidates_status",
        ),
        default="new",
        nullable=False,
        index=True,
    )

    bundle: Mapped[ContextBundle | None] = relationship(
        back_populates="candidate",
        uselist=False,
        cascade="all, delete-orphan",
    )
    drafts: Mapped[list[Draft]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class SeenHash(Base):
    """Dedup ledger — SHA-256 of (canonical_url + normalized_title)."""

    __tablename__ = "seen_hashes"

    hash: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContextBundle(Base):
    """Full fetched context for a candidate (main text + sublinks + PDFs)."""

    __tablename__ = "context_bundles"

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True
    )
    main_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sublinks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdfs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="bundle")


class Draft(Base):
    """LLM-generated draft for a candidate, awaiting editor decision."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    section: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    jurisdiction: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_decision: Mapped[str | None] = mapped_column(String, nullable=True, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    candidate: Mapped[Candidate] = relationship(back_populates="drafts")


class AuditLog(Base):
    """Append-only audit trail. One row per LLM call or editor action."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    fallback_triggered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# --- Engine / session management ---


@event.listens_for(Engine, "connect")
def _enable_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
    """Enable FK enforcement + WAL mode for SQLite connections."""
    # SQLite only — skip other dialects.
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def _build_engine(db_path: Path | None = None) -> Engine:
    path = db_path or get_settings().paths.db
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path.as_posix()}"
    return create_engine(url, future=True, echo=False)


_engine: Engine | None = None
_Session: sessionmaker[Any] | None = None


def _get_engine() -> Engine:
    global _engine, _Session  # noqa: PLW0603 — deliberate module-level singleton
    if _engine is None:
        _engine = _build_engine()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db(db_path: Path | None = None) -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    global _engine, _Session  # noqa: PLW0603 — deliberate module-level singleton
    _engine = _build_engine(db_path)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)


@contextmanager
def get_session() -> Iterator[Any]:
    """Transactional session context — commits on success, rolls back on exception."""
    _get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
