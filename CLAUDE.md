# CLAUDE.md

This file provides guidance to Claude Code when working on this repository. Read it before making any changes.

---

## Project Overview

**Name:** Regulatory Newsfeed Drafting Automation (POC)
**Goal:** Automate the weekly production of regulatory newsfeeds (starting with Water) that currently takes editors hours of manual work. The pipeline ingests regulatory websites, legislative PDFs, and saved HTML/Markdown files, then produces a publishable `.docx` newsfeed matching the exact format of `Water_Newsfeed.md`.

**Ground truth files (read these first, every time):**
- `reference/Water_Newsfeed.md` — the target output format. Every draft must match this structure.
- `reference/Water_Newsfeed_Template.md` — the URL registry and per-section/jurisdiction example stubs.

**Critical constraint:** This is a regulatory product. Hallucinated facts (dates, act numbers, agency names, quotes) are unacceptable. Every quoted phrase in a draft MUST appear verbatim in the source text. Validation is enforced by code, not trust.

---

## Tech Stack (fixed — do not substitute)

### External APIs (the only non-local dependencies)
- **AWS Bedrock** (primary): `anthropic.claude-3-5-sonnet-20241022-v2:0` (quality), `anthropic.claude-3-5-haiku-20241022-v1:0` (cheap)
- **OpenAI** (fallback only): `gpt-4o` (quality), `gpt-4o-mini` (cheap)

### Everything else is local / open-source
| Purpose | Tool |
|---|---|
| Orchestration | Prefect 2.x (self-hosted) |
| HTTP (async) | httpx |
| JS-rendered pages | Playwright |
| HTML parsing | selectolax |
| HTML → clean text | trafilatura |
| Markdown parsing | markdown-it-py |
| RSS / sitemap | feedparser, lxml |
| PDF text | pdfplumber |
| Scanned PDF OCR | ocrmypdf (wraps Tesseract) |
| Folder watcher | watchdog |
| Email (optional) | imap-tools |
| State + audit + dedup | SQLite (stdlib) + SQLAlchemy |
| Retries | tenacity |
| Config | PyYAML + Pydantic |
| Secrets | python-dotenv (`.env` file) |
| Prompt templates | Jinja2 |
| Schema validation | Pydantic |
| Word output | python-docx |
| Review UI | Streamlit |
| Logging | loguru |
| Containerization | Docker + docker-compose |

### Do NOT introduce
- ❌ Any AWS service other than Bedrock (no Lambda, S3, DynamoDB, SES, Fargate, etc.)
- ❌ Any Microsoft service (no SharePoint, Power Automate, Azure OpenAI)
- ❌ Scrapy (conflicts with Prefect orchestration)
- ❌ Selenium (Playwright is superior)
- ❌ BeautifulSoup (selectolax is 10x faster)
- ❌ Paid scraping APIs (overkill for government sites)
- ❌ Postgres, Redis, or any server-based DB (SQLite is sufficient for POC)

---

## Repository Layout

```
regulatory-newsfeed-poc/
├── CLAUDE.md                      ← this file
├── README.md                      ← human-facing setup guide
├── config.yaml                    ← runtime config (lookback, thresholds, model IDs)
├── .env                           ← BEDROCK_* and OPENAI_API_KEY (never commit)
├── .env.example                   ← committed template
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml                 ← uv / pip deps
├── requirements.txt               ← pinned versions
│
├── reference/                     ← ground-truth examples (DO NOT EDIT)
│   ├── Water_Newsfeed.md
│   └── Water_Newsfeed_Template.md
│
├── inbox/                         ← drop Lolex PDFs here (watched folder)
├── data/
│   ├── pipeline.db                ← SQLite: candidates, bundles, drafts, audit
│   ├── source_registry.json       ← parsed URL registry
│   └── cache/                     ← raw HTML/PDF cache keyed by SHA-256
├── outputs/
│   └── YYYY-MM-DD/
│       ├── Water_Newsfeed.docx
│       ├── Water_Newsfeed.md
│       └── horizon_payload.json
│
├── src/newsfeed/
│   ├── __init__.py
│   ├── config.py                  ← Pydantic settings loader
│   ├── db.py                      ← SQLAlchemy models + session
│   ├── llm_client.py              ← Bedrock primary + OpenAI fallback
│   ├── stage0_parse_template.py
│   ├── stage1_discovery.py        ← branches by url / html / md
│   ├── stage2_pdf_ingest.py       ← watchdog + pdfplumber + ocrmypdf
│   ├── stage3_dedup.py
│   ├── stage4_fetch.py            ← httpx + Playwright + trafilatura
│   ├── stage5_relevance.py        ← LLM binary gate
│   ├── stage6_classify.py         ← section + jurisdiction + content_type
│   ├── stage7_draft.py            ← Jinja2 + LLM slot-filling
│   ├── stage8_validate.py         ← hallucination guard + schema checks
│   ├── stage9_assemble.py         ← group by section/jurisdiction
│   ├── stage11_render.py          ← python-docx output
│   ├── stage12_publish.py         ← Horizon API + audit log
│   ├── templates/                 ← 6 Jinja2 draft templates
│   │   ├── new_plan.j2
│   │   ├── media_release.j2
│   │   ├── weekly_report.j2
│   │   ├── bullet_list.j2
│   │   ├── simple_update.j2
│   │   └── gazette_bundle.j2
│   └── prompts/                   ← LLM prompt templates (versioned)
│       ├── relevance_v1.j2
│       ├── classify_v1.j2
│       └── draft_v1.j2
│
├── ui/
│   └── streamlit_app.py           ← editor review + dashboards
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                  ← saved HTML/PDF samples for tests
│
└── main.py                        ← Prefect flow entry point
```

---

## Pipeline Stages (end-to-end)

The pipeline is a **linear, human-in-the-loop** flow. Deterministic code does mechanical work; LLMs are called only at explicit decision points.

### Stage 0 — Parse template, build source registry
**Input:** `reference/Water_Newsfeed_Template.md`
**Output:** `data/source_registry.json`
Parse the template with `markdown-it-py`. For each URL found, capture: `(url, section, jurisdiction, source_label, example_template)`. Also support local `.html` and `.md` files as sources via explicit entries in a supplementary registry.

### Stage 1 — Discovery
Branch by source type:
- **URL:** try RSS → sitemap → listing HTML (in that order). Use `httpx` first; fall back to Playwright only if the listing requires JS.
- **Local `.html`:** parse with `selectolax`.
- **Local `.md`:** parse with `markdown-it-py`.

Apply lookback window from `config.yaml` (`lookback_days`: 7 / 14 / 21 / 28 / any int). Items without parseable dates → flag for editor review, **do not silently drop**.

Persist candidates to SQLite table `candidates`.

### Stage 2 — Lolex PDF ingestion (parallel)
`watchdog` monitors `./inbox/`. On new PDF:
1. SHA-256 → check `seen_hashes`
2. `pdfplumber` for text extraction
3. If text layer empty → `ocrmypdf` (Tesseract)
4. Regex-split into legislative items
5. Merge into `candidates`

Optional: `imap-tools` polls an IMAP inbox and saves attachments to `./inbox/`.

### Stage 3 — Deduplication
SHA-256 of `(canonical_url + normalized_title)`. SQLite `seen_hashes` table. Drop seen items. TTL pruning after `lookback_days × 4`.

### Stage 4 — Fetch full content
For each candidate:
1. Fetch article (skip if already local `.html`/`.md`)
2. `trafilatura` strips boilerplate
3. Extract sublinks matching site profile patterns (ministerial pages, PDFs, gazettes)
4. Fetch sublinks (depth = 1, configurable)
5. Cache to `./data/cache/{hash}.{ext}`

Build `context_bundle`: main text + sublinks + PDFs + metadata. Store as JSON in SQLite.

### Stage 5 — LLM relevance filter (binary gate)
**Model tier:** `cheap` (Haiku / gpt-4o-mini).
Prompt returns `{relevant: bool, reason: str}`. Drop irrelevant items with logged reason.

### Stage 6 — LLM classification
**Model tier:** `quality` (Sonnet / gpt-4o).
Inject: 9 section definitions, 1-2 examples per section from the template, source→section hint. Returns `{section, jurisdiction, confidence, content_type}`.
- `content_type ∈ {new_plan, media_release, gazette_notice, simple_update, bullet_list, weekly_report}`
- `confidence < 0.7` → editor queue.

### Stage 7 — Draft generation
**Model tier:** `quality`.
Select one of 6 Jinja2 templates based on `content_type`. LLM fills slots using ONLY facts from `context_bundle`. Quotes MUST appear verbatim in source.

### Stage 8 — Hallucination guard + schema validation
Pure Python. Checks:
- Every quoted phrase is substring of source text
- Every date appears verbatim in source
- Act names/numbers match Lolex extraction
- Agency name ∈ known list
- Section ∈ 9 valid sections
- Jurisdiction ∈ 8 valid jurisdictions
- Source URL reachable (HEAD request)

Failures → editor queue with diff, never auto-publish.

### Stage 9 — Assembly
Group drafts by section (fixed order 1-9) then jurisdiction (fixed order: National, VIC, NSW, QLD, WA, SA, TAS, ACT, NT). Empty combos are **omitted**, not rendered as blanks. Append Upcoming Dates, Previous Newsfeeds, Copyright footer.

### Stage 10 — Editor review UI
Streamlit at `localhost:8501`. Reads/writes SQLite. Shows draft, source links, confidence, side-by-side source vs draft. Actions: Approve | Edit | Reject. Desktop notifications via `plyer`.

### Stage 11 — Render
`python-docx` generates `.docx` matching `Water_Newsfeed.md` format exactly: bold titles, italic source attributions, section headings, jurisdiction sub-headings, bulleted lists. Also save `.md` archive and `horizon_payload.json`.

### Stage 12 — Publish + audit
POST to Horizon CMS via `httpx`. Optional SMTP email via `smtplib`. Write audit record to SQLite `audit_log`:
`run_id, timestamp, prompt_version, model, provider (bedrock/openai), source_urls, editor_decision, draft_hash, token_cost, fallback_triggered`.

---

## The Six Draft Templates (content types)

Derived directly from patterns in `reference/Water_Newsfeed.md`. Use these exact structures — do not invent new ones.

### TYPE 1 — `new_plan` (legislative instruments)
```
**{Title}**
The {plan_name} ({jurisdiction}) has been made under the authority of the {parent_act}.
The Plan:
- {bullet_1};
- {bullet_2}; and
- {bullet_3}.
The Plan commenced on {date}.
*(Source: Lawlex Legislative Alert & Premium Research)*
```

### TYPE 2 — `media_release` (with quote)
```
**{Title}**
{Agency} has announced that "[{quote_1}]".
According to {Agency}, "[{quote_2}]".
{Agency}'s media release ({date})
*(Source: {Agency})*
```

### TYPE 3 — `weekly_report`
```
**{Report Name}**
{Agency} has made available its latest {report_name} for the week ending {date}, which provides information on {topics}.
*(Source: {Agency})*
```

### TYPE 4 — `bullet_list` (multiple releases at once)
```
**{Agency} Media Releases**
{Agency} has made available the following media releases:
- {item_1} ({date});
- {item_2} ({date}); and
- {item_3} ({date}).
*(Source: {Agency})*
```

### TYPE 5 — `simple_update`
```
**{Title}**
{Agency} has made available an update - {subtitle} ({date}), which advises that "[{quote}]".
*(Source: {Agency})*
```

### TYPE 6 — `gazette_bundle`
```
**Gazette Notices**
Various notices have been issued in Government Gazette No. {number} ({date}), pp. {pages}, under the {act}, regarding the:
- {notice_1};
- {notice_2}; and
- {notice_3}.
*(Source: {gazette}; Lawlex Legislative Alert & Premium Research)*
```

---

## LLM Client Contract

All LLM calls go through `src/newsfeed/llm_client.py`. Never call `boto3` or `openai` directly from pipeline stages.

```python
class LLMClient:
    def complete(
        self,
        prompt: str,
        schema: type[BaseModel],   # Pydantic model for structured output
        model_tier: Literal["cheap", "quality"],
        max_retries: int = 2,
    ) -> BaseModel:
        """
        Try Bedrock first. On ThrottlingException, timeout, ServiceUnavailable,
        or JSON parse failure → fall back to OpenAI. Log provider used,
        latency, token counts, and fallback flag to SQLite audit_log.
        """
```

Structured output:
- Bedrock → Converse API with `tool_use` forcing a schema-shaped tool call.
- OpenAI → `response_format={"type": "json_schema", "json_schema": ...}`.

Always validate the returned dict with the Pydantic schema before returning. If validation fails after retries on both providers, raise `LLMStructuredOutputError` and route the item to the editor queue.

---

## SQLite Schema (minimum)

```sql
CREATE TABLE candidates (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_type TEXT CHECK(source_type IN ('url','html','md','pdf')),
    url TEXT,
    title TEXT,
    pub_date DATE,
    section_hint TEXT,
    jurisdiction_hint TEXT,
    source_label TEXT,
    hash TEXT UNIQUE NOT NULL,
    discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'new'  -- new|fetched|filtered|classified|drafted|validated|review|approved|rejected|published
);

CREATE TABLE seen_hashes (
    hash TEXT PRIMARY KEY,
    first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    source_id TEXT,
    url TEXT
);

CREATE TABLE context_bundles (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id),
    main_text TEXT,
    sublinks_json TEXT,
    pdfs_json TEXT,
    metadata_json TEXT
);

CREATE TABLE drafts (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidates(id),
    content_type TEXT,
    section TEXT,
    jurisdiction TEXT,
    confidence REAL,
    draft_text TEXT,
    edited_text TEXT,
    validation_errors_json TEXT,
    editor_decision TEXT,  -- approve|edit|reject|pending
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    stage TEXT,
    candidate_id INTEGER,
    prompt_version TEXT,
    model TEXT,
    provider TEXT,             -- bedrock | openai
    fallback_triggered INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Configuration (`config.yaml`)

```yaml
pipeline:
  lookback_days: 7            # editable: 7, 14, 21, 28, ...
  max_sublink_depth: 1
  confidence_threshold: 0.7
  cache_retention_days: 28

paths:
  inbox: ./inbox
  cache: ./data/cache
  outputs: ./outputs
  db: ./data/pipeline.db
  source_registry: ./data/source_registry.json
  reference: ./reference

llm:
  primary: bedrock
  fallback: openai
  timeout_seconds: 10
  max_retries: 2

bedrock:
  region: ap-southeast-2
  cheap_model: anthropic.claude-3-5-haiku-20241022-v1:0
  quality_model: anthropic.claude-3-5-sonnet-20241022-v2:0

openai:
  cheap_model: gpt-4o-mini
  quality_model: gpt-4o

sections:
  - Water Reform
  - Urban Water Supply and Use
  - Rural Water Supply and Use
  - Wastewater and Water Reuse
  - Collection
  - Conservation
  - Water Rights
  - Waterways and Flood Mitigation
  - General Water Quality and Sustainability

jurisdictions:
  - National
  - Victoria
  - New South Wales
  - Queensland
  - Western Australia
  - South Australia
  - Tasmania
  - Australian Capital Territory
  - Northern Territory
```

Load via Pydantic `BaseSettings` in `src/newsfeed/config.py`. Never read `config.yaml` directly from stage modules — always import the settings object.

---

## Development Workflow

### Setup
```bash
# Python 3.11+
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env            # then fill in AWS + OpenAI keys
```

### Running locally
```bash
# Full pipeline (one-off run)
python main.py run --lookback-days 7

# Just discovery
python main.py discover

# Just PDF watcher (runs until Ctrl+C)
python main.py watch-inbox

# Editor UI
streamlit run ui/streamlit_app.py

# Prefect orchestration UI
prefect server start    # in one terminal
python main.py          # in another — deploys the flow
```

### Docker
```bash
docker-compose up       # runs pipeline + Streamlit + Prefect server
```

### Tests
```bash
pytest tests/unit -v
pytest tests/integration -v    # may need --net=host for live fetches
```

---

## Coding Standards

1. **One stage per file.** Each `stageN_*.py` exposes exactly one `run(...)` function. Stages communicate through SQLite, not direct imports.
2. **Pydantic everywhere there's structured data.** Candidates, bundles, drafts, LLM outputs — all Pydantic models. No raw dicts across module boundaries.
3. **Loguru for logging.** `from loguru import logger`. Structured logs: `logger.info("fetched article", url=..., bytes=...)`.
4. **Tenacity for all network retries.** Never hand-roll retry loops.
5. **Async where it helps.** Discovery and fetch stages use `httpx.AsyncClient` with a bounded semaphore (default 8 concurrent). LLM calls are sync (ordering matters for audit).
6. **Type hints mandatory.** Run `mypy src/` clean.
7. **Formatting:** `ruff format` + `ruff check --fix`. No other formatters.
8. **No `print()`.** Use loguru.
9. **No hardcoded paths.** Read from `settings.paths.*`.
10. **No secrets in code or logs.** Loguru redaction for anything key-shaped.

---

## Testing Strategy

- **Unit tests** cover parsers (template parser, PDF extractor, date parser), validators (hallucination guard), template renderers (each of the 6 Jinja2 templates against golden outputs).
- **Integration tests** run the full pipeline against fixture HTML/PDF in `tests/fixtures/` with the LLM client mocked. Golden output compared against `reference/Water_Newsfeed.md` structure (not word-for-word — structural equivalence).
- **LLM contract tests** hit real Bedrock with a tiny prompt to verify credentials + structured output shape. Marked `@pytest.mark.live` — skipped by default.
- **Target coverage:** 80% on `src/newsfeed/`. Validators and templates should be at 100%.

---

## Hallucination Prevention (non-negotiable rules)

1. **Quotes are substrings.** Before a draft passes Stage 8, every substring inside `"[...]"` must appear verbatim in `context_bundle.main_text` or a sublink. Implement via `str.find()` — no fuzzy matching.
2. **Dates are extracted, not inferred.** Parse dates with `dateparser` from source text first; pass them as context to the LLM. Reject drafts where a date in the output is not found in the source.
3. **Act numbers / plan numbers are regex-extracted** from Lolex PDFs before the LLM sees them. The LLM fills the `{plan_name}` slot — it does not invent the number.
4. **Agency names come from a whitelist.** `config.yaml` has a known agency list per jurisdiction. LLM picks from the list; free-form agency strings are rejected.
5. **If in doubt, send to editor.** The pipeline's default failure mode is "human reviews it", never "publish anyway".

---

## Common Tasks (quick reference)

### "Add a new regulator site"
1. Add a new entry to `data/source_registry.json` with `section`, `jurisdiction`, `source_label`.
2. If the site needs JS rendering, add `"requires_js": true` to the entry.
3. If sublinks follow a non-standard pattern, add `"sublink_patterns": ["/ministers/", "\\.pdf$"]`.
4. Run `python main.py discover --source-id <new_id>` to test.

### "Add a new content type"
1. Add a new Jinja2 template in `src/newsfeed/templates/`.
2. Add the type to the `content_type` enum in Pydantic schemas.
3. Update the classification prompt in `src/newsfeed/prompts/classify_v1.j2` with 1-2 examples.
4. Add a golden-output test in `tests/unit/test_templates.py`.

### "Change lookback window"
Edit `config.yaml` → `pipeline.lookback_days`. No code changes.

### "Debug why an article didn't appear"
```bash
sqlite3 data/pipeline.db "SELECT id, status, url FROM candidates WHERE url LIKE '%<keyword>%';"
sqlite3 data/pipeline.db "SELECT * FROM audit_log WHERE candidate_id = <id> ORDER BY timestamp;"
```

### "Force re-process an item"
```bash
sqlite3 data/pipeline.db "DELETE FROM seen_hashes WHERE hash = '<hash>';"
sqlite3 data/pipeline.db "UPDATE candidates SET status = 'new' WHERE id = <id>;"
```

---

## POC Scope Boundaries (what's IN / OUT)

**IN for POC:**
- Water sector only
- Jurisdictions: Victoria, NSW, National (≈70% coverage)
- Content types: `media_release`, `new_plan`, `weekly_report` first; others as time allows
- Editor approves 100% of drafts (no auto-publish)
- Single user, local run

**OUT for POC (future phases):**
- Other 14 newsfeeds (Insurance, Infrastructure, etc.)
- Auto-publish on high confidence
- Multi-user review / role-based permissions
- Live cloud deployment (stay local)
- Translation / multi-language
- Anything requiring SharePoint, Power Automate, or Azure services

---

## Definition of Done (per stage)

A stage is "done" when:
1. Its `run()` function is covered by unit tests with ≥80% line coverage.
2. It logs entry, key decisions, and exit to loguru.
3. All DB writes are in a single transaction per item.
4. It produces a Prefect task that other stages can depend on.
5. `mypy src/newsfeed/stageN_*.py` is clean.
6. A README snippet in the stage module's docstring explains inputs, outputs, and failure modes.

---

## When You (Claude Code) Are Asked To Work On This

**Before writing any code:**
1. Read `reference/Water_Newsfeed.md` and `reference/Water_Newsfeed_Template.md` if the task touches output format or source discovery.
2. Check `data/pipeline.db` schema if the task touches persistence.
3. Verify `config.yaml` for any configurable values before hardcoding.
4. Look at the existing stage modules for conventions.

**Prefer:**
- Small, incremental edits over rewrites.
- Adding new stages/modules over modifying existing ones when scope is unclear.
- Asking to clarify scope if a request could plausibly go IN or OUT of POC scope.

**Never:**
- Introduce a tool not listed in "Tech Stack".
- Bypass the `LLMClient` abstraction.
- Silently drop candidates — always log + store with a status.
- Write to `reference/` — it's read-only ground truth.
- Auto-approve drafts that failed Stage 8 validation.
- Commit `.env`, `data/pipeline.db`, or anything under `outputs/`.

**Always:**
- Add a row to `audit_log` whenever an LLM is called or an editor acts.
- Use Pydantic models for cross-module data.
- Write a unit test alongside new logic.
- Update this `CLAUDE.md` if you add a new stage, content type, or config key.

---

## Quick Smoke Test (run after any significant change)

```bash
# 1. Parse template — should produce source_registry.json with >= 50 entries
python -m src.newsfeed.stage0_parse_template

# 2. Run discovery on 3 known-good sources
python main.py discover --limit 3

# 3. Verify SQLite populated
sqlite3 data/pipeline.db "SELECT COUNT(*) FROM candidates WHERE status='new';"

# 4. End-to-end on a single fixture
pytest tests/integration/test_full_pipeline.py::test_single_media_release -v

# 5. Generate a sample .docx from existing approved drafts
python main.py render --date $(date +%Y-%m-%d)

# 6. Open the result and eyeball against reference/Water_Newsfeed.md
```

If all 6 pass, the pipeline is healthy.
