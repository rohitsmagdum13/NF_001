# Regulatory Newsfeed Drafting Automation (POC)

Automates weekly production of regulatory newsfeeds (starting with Water).
Ingests regulator websites, legislative PDFs, and local HTML/Markdown, and
drafts a publishable `.docx` newsfeed matching the format of
[`reference/Water_Newsfeed.md`](reference/Water_Newsfeed.md).

> See [CLAUDE.md](CLAUDE.md) for the full spec — pipeline stages, tech-stack
> constraints, data model, and non-negotiable hallucination rules.

---

## Setup

Requires **Python 3.12+**.

```bash
# 1. Create virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies (uv recommended)
uv pip install -e ".[dev]"
# or: pip install -e ".[dev]"

# 3. Install Playwright browser
playwright install chromium

# 4. Configure secrets
cp .env.example .env               # then fill in AWS Bedrock + OpenAI keys
```

---

## Adding sources

Drop a `.txt` file into `data/local_articles/` — one URL per line, `#` for comments.

**Naming convention** — `Section__Jurisdiction__description.txt`  
Sets the section and jurisdiction for every URL in the file:

```
data/local_articles/
  Water_Reform__New_South_Wales__test-sources.txt
  Urban_Water_Supply_and_Use__Victoria__sources.txt
```

**Example file contents:**

```
# Water Reform — NSW sources
https://www.waternsw.com.au/community-news/media-releases
https://www.environment.nsw.gov.au/news
```

Valid sections (from `config.yaml`):

- Water Reform
- Urban Water Supply and Use
- Rural Water Supply and Use
- Wastewater and Water Reuse
- Collection
- Conservation
- Water Rights
- Waterways and Flood Mitigation
- General Water Quality and Sustainability

Valid jurisdictions: `National`, `Victoria`, `New South Wales`, `Queensland`,
`Western Australia`, `South Australia`, `Tasmania`, `Australian Capital Territory`, `Northern Territory`

---

## Running the pipeline

### Full end-to-end run

```bash
uv run python scripts/run_pipeline.py
```

### Common options

```bash
# Override the run date
uv run python scripts/run_pipeline.py --date 2026-04-23

# Override lookback window (default: from config.yaml)
uv run python scripts/run_pipeline.py --lookback-days 14

# Run from a specific stage (skips earlier stages)
uv run python scripts/run_pipeline.py --from-stage 5

# Stop after a specific stage
uv run python scripts/run_pipeline.py --stop-after 4

# Re-assemble including already-published candidates
uv run python scripts/run_pipeline.py --from-stage 9 --include-published

# Verbose debug logging
uv run python scripts/run_pipeline.py --log-level DEBUG
```

### Run a single stage

```bash
# Stage 0 — Parse template / build source registry
uv run python -m newsfeed.stage0_parse_template

# Stage 1 — Discovery
uv run python -m newsfeed.stage1_discovery

# Stage 3 — Deduplication
uv run python -m newsfeed.stage3_dedup

# Stage 4 — Fetch full content
uv run python -m newsfeed.stage4_fetch

# Stage 5 — LLM relevance filter
uv run python -m newsfeed.stage5_relevance

# Stage 6 — LLM classification
uv run python -m newsfeed.stage6_classify

# Stage 7 — Draft generation
uv run python -m newsfeed.stage7_draft

# Stage 8 — Hallucination guard + validation
uv run python -m newsfeed.stage8_validate

# Stage 9 — Assemble payload
uv run python -m newsfeed.stage9_assemble
uv run python -m newsfeed.stage9_assemble --date 2026-04-23 --include-published

# Stage 11 — Render .docx + .md
uv run python -m newsfeed.stage11_render
uv run python -m newsfeed.stage11_render --date 2026-04-23
```

### Partial / resume runs

```bash
# Re-run only the LLM stages (skip discovery + fetch)
uv run python scripts/run_pipeline.py --from-stage 5 --stop-after 8

# Regenerate output files from an existing payload
uv run python scripts/run_pipeline.py --from-stage 11 --date 2026-04-23

# Re-assemble + re-render after editing drafts in the UI
uv run python scripts/run_pipeline.py --from-stage 9 --date 2026-04-23
```

---

## Editor review UI

```bash
uv run streamlit run ui/streamlit_app.py
# Opens at http://localhost:8501
```

---

## Database inspection

```bash
# Candidate status breakdown
sqlite3 data/pipeline.db "SELECT status, COUNT(*) FROM candidates GROUP BY status;"

# Find a specific article
sqlite3 data/pipeline.db "SELECT id, status, title, url FROM candidates WHERE url LIKE '%keyword%';"

# See audit log for a candidate
sqlite3 data/pipeline.db "SELECT * FROM audit_log WHERE candidate_id = <id> ORDER BY timestamp;"

# Force re-process a candidate
sqlite3 data/pipeline.db "DELETE FROM seen_hashes WHERE hash = '<hash>';"
sqlite3 data/pipeline.db "UPDATE candidates SET status = 'new' WHERE id = <id>;"

# Clear all stale candidates (start fresh)
sqlite3 data/pipeline.db "DELETE FROM candidates; DELETE FROM seen_hashes;"
```

---

## Configuration

Edit [`config.yaml`](config.yaml) to change runtime behaviour:

| Key | Default | Description |
|-----|---------|-------------|
| `pipeline.lookback_days` | `7` | How far back to look for articles |
| `pipeline.confidence_threshold` | `0.7` | Min LLM confidence to auto-pass |
| `pipeline.fetch_concurrency` | `8` | Concurrent HTTP fetches |
| `pipeline.http_timeout_seconds` | `15` | Per-request HTTP timeout |
| `llm.primary` | `bedrock` | Primary LLM provider |
| `llm.fallback` | `openai` | Fallback LLM provider |

Override `lookback_days` without editing the file:

```bash
uv run python scripts/run_pipeline.py --lookback-days 14
```

---

## Logs

| Location | Contents |
|----------|----------|
| `outputs/pipeline.log` | Full DEBUG log, 10 MB rotation, 5 files retained |
| stderr | INFO+ console output with colour and timing |

```bash
# Tail the log file
tail -f outputs/pipeline.log

# Show only errors
grep " ERROR " outputs/pipeline.log
```

---

## Outputs

Each run writes to `outputs/YYYY-MM-DD/`:

```
outputs/2026-04-23/
  Water_Newsfeed.docx        ← formatted Word document
  Water_Newsfeed.md          ← plain-text archive
  horizon_payload.json       ← structured JSON (AssembledNewsfeed)
```

If `Water_Newsfeed.docx` is open in Word when the pipeline runs, the render
stage writes a timestamped fallback: `Water_Newsfeed_143022.docx`.

---

## Development

```bash
# Lint + format
ruff check --fix src tests && ruff format src tests

# Type check
mypy src

# Unit tests
pytest tests/unit -v

# Integration tests (uses fixture HTML/PDFs, mocks LLM)
pytest tests/integration -v

# Coverage report
pytest --cov=src --cov-report=term-missing

# Security scan
bandit -r src
```

---

## Docker

```bash
docker-compose up --build
```

Ports:
- Prefect UI: http://localhost:4200
- Streamlit UI: http://localhost:8501

---

## Project layout

```
.
├── CLAUDE.md                     ← spec & working rules (read first)
├── README.md                     ← this file
├── config.yaml                   ← runtime config (non-secret)
├── .env.example                  ← secrets template
├── pyproject.toml / requirements.txt
├── Dockerfile / docker-compose.yml
├── scripts/
│   └── run_pipeline.py           ← end-to-end pipeline runner
├── main.py                       ← CLI + Prefect flow entry
├── reference/                    ← ground-truth newsfeed + template (read-only)
├── inbox/                        ← drop Lolex PDFs here
├── data/
│   ├── pipeline.db               ← SQLite: candidates, bundles, drafts, audit
│   ├── source_registry.json      ← generated by Stage 0
│   ├── cache/                    ← raw HTML/PDF cache (gitignored)
│   └── local_articles/           ← drop .txt / .html / .md source files here
├── outputs/
│   └── YYYY-MM-DD/
│       ├── Water_Newsfeed.docx
│       ├── Water_Newsfeed.md
│       └── horizon_payload.json
├── src/newsfeed/
│   ├── config.py
│   ├── db.py
│   ├── llm_client.py
│   ├── stage0_parse_template.py
│   ├── stage1_discovery.py
│   ├── stage3_dedup.py
│   ├── stage4_fetch.py
│   ├── stage5_relevance.py
│   ├── stage6_classify.py
│   ├── stage7_draft.py
│   ├── stage8_validate.py
│   ├── stage9_assemble.py
│   ├── stage11_render.py
│   ├── templates/                ← Jinja2 draft templates (6 content types)
│   └── prompts/                  ← LLM prompt templates (versioned)
├── ui/streamlit_app.py           ← editor review UI
└── tests/unit · tests/integration · tests/fixtures
```
