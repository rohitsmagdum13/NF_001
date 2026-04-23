# Regulatory Newsfeed Drafting Automation (POC)

Automates weekly production of regulatory newsfeeds (starting with Water).
Ingests regulator websites, legislative PDFs, and local HTML/Markdown, and
drafts a publishable `.docx` newsfeed matching the format of
[`reference/Water_Newsfeed.md`](reference/Water_Newsfeed.md).

> See [CLAUDE.md](CLAUDE.md) for the full spec — pipeline stages, tech-stack
> constraints, data model, and non-negotiable hallucination rules.

---

## Current status

**Scaffolding only.** No pipeline stages are wired yet. This commit gives you:

- Project layout (`src/newsfeed/`, `tests/`, `inbox/`, `data/`, `outputs/`, `ui/`)
- Dependency pins ([`pyproject.toml`](pyproject.toml), [`requirements.txt`](requirements.txt))
- Runtime config: [`config.yaml`](config.yaml), [`.env.example`](.env.example)
- Pydantic settings loader — [`src/newsfeed/config.py`](src/newsfeed/config.py)
- SQLAlchemy models for the 4 pipeline tables — [`src/newsfeed/db.py`](src/newsfeed/db.py)
- Bedrock + OpenAI fallback LLM client — [`src/newsfeed/llm_client.py`](src/newsfeed/llm_client.py)
- CLI + Prefect flow stub — [`main.py`](main.py)
- Docker / compose for Prefect server, pipeline, watcher, Streamlit UI
- Smoke tests — [`tests/unit/test_scaffold.py`](tests/unit/test_scaffold.py)

---

## Setup

Requires **Python 3.12+**.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env                # then fill in AWS + OpenAI keys
```

Smoke test:

```bash
pytest tests/unit -v
python main.py run --lookback-days 7   # runs stage stubs only
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

## Running individual commands

```bash
python main.py run --lookback-days 7         # full pipeline (stubbed)
python main.py discover --limit 3            # Stage 1 only
python main.py watch-inbox                   # Stage 2 PDF watcher
python main.py render --date 2026-04-22      # Stage 11 render
streamlit run ui/streamlit_app.py            # Editor review UI
```

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
├── main.py                       ← CLI + Prefect flow entry
├── reference/                    ← ground-truth newsfeed + template (read-only)
├── inbox/                        ← drop Lolex PDFs here
├── data/                         ← SQLite + cache (gitignored)
├── outputs/                      ← generated .docx / .md / payloads
├── src/newsfeed/
│   ├── config.py
│   ├── db.py
│   ├── llm_client.py
│   ├── stage*.py                 ← stages land here incrementally
│   ├── templates/                ← Jinja2 draft templates (6 content types)
│   └── prompts/                  ← LLM prompt templates (versioned)
├── ui/streamlit_app.py           ← editor review UI
└── tests/unit · tests/integration · tests/fixtures
```

---

## Development

- **Lint / format:** `ruff check --fix src tests && ruff format src tests`
- **Type check:** `mypy src`
- **Tests:** `pytest tests/unit -v`
- **Coverage:** `pytest --cov=src --cov-report=term-missing`
- **Security scan:** `bandit -r src`

---

## What's next

Upcoming increments land one stage at a time, in order:

1. **Stage 0** — Parse `reference/Water_Newsfeed_Template.md` → `data/source_registry.json`.
2. **Stage 1** — Discovery (RSS → sitemap → listing HTML).
3. **Stage 2** — PDF watcher on `./inbox/`.
4. **Stages 3–9** — Dedup → fetch → relevance → classify → draft → validate → assemble.
5. **Stage 10** — Streamlit review UI.
6. **Stages 11–12** — `.docx` render + publish + audit.
