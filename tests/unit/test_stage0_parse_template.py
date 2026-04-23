"""Unit tests for Stage 0 — template parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from newsfeed.config import get_settings, load_settings
from newsfeed.schemas import SourceRegistry
from newsfeed.stage0_parse_template import parse_template, run

FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_template.md"
REAL_TEMPLATE = Path(__file__).parents[2] / "reference" / "Water_Newsfeed_Template.md"


@pytest.mark.unit
def test_parse_fixture_extracts_urls() -> None:
    registry = parse_template(FIXTURE)

    urls = [s.url for s in registry.sources]
    assert "https://example.com/vic-news" in urls
    assert "https://example.com/vic-other-news" in urls
    assert "https://example.com/nsw-news" in urls


@pytest.mark.unit
def test_parse_fixture_skips_invalid_jurisdictions_and_sections() -> None:
    registry = parse_template(FIXTURE)
    urls = [s.url for s in registry.sources]

    # "International" is outside the configured jurisdictions list
    assert "https://example.com/international-should-be-skipped" not in urls

    # Section 10 (Upcoming Dates) is not a pipeline section
    assert "https://example.com/should-be-skipped-section-10" not in urls


@pytest.mark.unit
def test_parse_fixture_captures_source_label() -> None:
    registry = parse_template(FIXTURE)

    vic_entries = [
        s for s in registry.sources if s.jurisdiction == "Victoria" and s.section == "Water Reform"
    ]
    assert vic_entries, "expected at least one Victoria/Water Reform entry"
    assert all(s.source_label == "Vic" for s in vic_entries)

    nsw_entries = [
        s
        for s in registry.sources
        if s.jurisdiction == "New South Wales" and s.section == "Water Reform"
    ]
    assert nsw_entries
    assert all(s.source_label == "NSW" for s in nsw_entries)


@pytest.mark.unit
def test_parse_fixture_captures_example_template() -> None:
    registry = parse_template(FIXTURE)
    examples = [
        e
        for e in registry.examples
        if e.section == "Urban Water Supply and Use" and e.jurisdiction == "Victoria"
    ]
    assert len(examples) == 1
    ex = examples[0]
    assert ex.title == "Latest Weekly Water Update"
    assert "Melbourne Water has made available" in ex.body
    assert ex.source_attribution == "Melbourne Water"


@pytest.mark.unit
def test_source_id_is_deterministic_and_unique_per_url() -> None:
    registry = parse_template(FIXTURE)
    ids = [s.source_id for s in registry.sources]
    # no duplicates
    assert len(ids) == len(set(ids))
    # stable on re-parse
    registry2 = parse_template(FIXTURE)
    assert [s.source_id for s in registry2.sources] == ids


@pytest.mark.unit
def test_parse_rejects_unknown_sections_when_whitelist_empty() -> None:
    with pytest.raises(ValueError, match="sections"):
        parse_template(FIXTURE, valid_sections=set(), valid_jurisdictions={"Victoria"})


@pytest.mark.unit
def test_run_writes_valid_json(tmp_path: Path) -> None:
    out = tmp_path / "source_registry.json"
    counts = run(template_path=FIXTURE, output_path=out)
    assert out.exists()
    assert counts["sources"] > 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    # SourceRegistry must round-trip
    registry = SourceRegistry.model_validate(payload)
    assert len(registry.sources) == counts["sources"]
    assert len(registry.examples) == counts["examples"]


@pytest.mark.unit
def test_run_raises_on_missing_template(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        run(template_path=missing, output_path=tmp_path / "out.json")


@pytest.mark.unit
def test_bold_label_without_body_is_not_emitted_as_example(tmp_path: Path) -> None:
    """Bare `**Reference Links:**` headers shouldn't become ExampleTemplates."""
    md = """# Test

## 1. Water Reform

### National - Water Reform

**Reference Links:**

- https://example.com/one
- https://example.com/two
"""
    fixture = tmp_path / "divider.md"
    fixture.write_text(md, encoding="utf-8")

    registry = parse_template(fixture)

    # URLs must still be captured
    assert len(registry.sources) == 2
    # But no example should be emitted for the empty-body bold divider
    assert not any(e.title == "Reference Links:" for e in registry.examples)


@pytest.mark.integration
def test_parse_real_template_produces_many_sources() -> None:
    """Smoke test against the actual template — should give ≥50 entries per CLAUDE.md."""
    if not REAL_TEMPLATE.exists():
        pytest.skip("real template not available")

    # Re-load settings to make sure sections/jurisdictions are populated
    get_settings.cache_clear()
    settings = load_settings()
    assert settings.sections and settings.jurisdictions

    registry = parse_template(REAL_TEMPLATE)
    assert len(registry.sources) >= 50, (
        f"expected ≥50 sources from real template, got {len(registry.sources)}"
    )
    # every entry must carry section + jurisdiction from our whitelists
    valid_sections = set(settings.sections)
    valid_jurisdictions = set(settings.jurisdictions)
    for s in registry.sources:
        assert s.section in valid_sections, s.section
        assert s.jurisdiction in valid_jurisdictions, s.jurisdiction
        assert s.url and s.url.startswith("http")
