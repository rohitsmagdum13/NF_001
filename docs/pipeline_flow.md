# Pipeline Technical & Logical Flow

---

## PART 1 — HIGH-LEVEL PIPELINE OVERVIEW

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║                    REGULATORY NEWSFEED PIPELINE — END TO END                        ║
║                                                                                      ║
║  INPUT SOURCES                         PIPELINE ENGINE                    OUTPUT     ║
║  ─────────────                         ───────────────                    ──────     ║
║                                                                                      ║
║  reference/                                                                          ║
║  Water_Newsfeed_Template.md  ──►  Stage 0   ──►  Stage 1  ──►  Stage 3              ║
║                                  (Registry)  (Discovery)  (Dedup)                   ║
║  data/local_articles/        ──►     │                                               ║
║  *.txt (URL lists)                   │                         │                     ║
║  *.html / *.md                       ▼                         ▼                     ║
║                                source_registry.json     candidates table             ║
║  inbox/                                                        │                     ║
║  *.pdf (Lolex)  ──► Stage 2 ──────────────────────────────────┘                     ║
║                   (PDF Ingest)                                 │                     ║
║                                                                ▼                     ║
║                                                           Stage 4                    ║
║                                                          (Fetch)                     ║
║                                                               │                      ║
║                                                               ▼                      ║
║                                                          Stage 5   ◄── gpt-4o-mini   ║
║                                                        (Relevance)     (cheap LLM)   ║
║                                                               │                      ║
║                                                    irrelevant │ relevant             ║
║                                                      (dropped)▼                      ║
║                                                          Stage 6   ◄── gpt-4o        ║
║                                                         (Classify)    (quality LLM)  ║
║                                                               │                      ║
║                                                    low conf.  │ high conf.           ║
║                                                  (editor queue)▼                     ║
║                                                          Stage 7   ◄── gpt-4o        ║
║                                                           (Draft)     (quality LLM)  ║
║                                                               │                      ║
║                                                               ▼                      ║
║                                                          Stage 8                     ║
║                                                        (Validate)                    ║
║                                                               │                      ║
║                                                    fail:      │ pass:                ║
║                                                  (editor queue)▼                     ║
║                                              ┌── Stage 9 (Assemble) ──┐             ║
║                                              │   Stage 10 (Review UI) │             ║
║                                              └──► Stage 11 (Render)  ◄┘             ║
║                                                        │                             ║
║                                              ┌─────────┴──────────┐                 ║
║                                              ▼                    ▼                  ║
║                                     Water_Newsfeed.docx   horizon_payload.json       ║
║                                     Water_Newsfeed.md                                ║
╚══════════════════════════════════════════════════════════════════════════════════════╝
```

---

## PART 2 — STAGE-BY-STAGE TECHNICAL DETAIL

---

### STAGE 0 — Parse Template / Build Source Registry

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 0 — Parse Template → source_registry.json                        │
│  Tool: markdown-it-py, Pydantic, json                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  INPUT (preferred)                   INPUT (fallback)                   │
│  ──────────────────                  ──────────────────                 │
│  reference/                          data/local_articles/               │
│  Water_Newsfeed_Template.md          *.txt (URL lists)                  │
│                                                                          │
│  LOGIC:                                                                  │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  Does reference/Water_Newsfeed_Template.md exist?        │            │
│  │          YES ──► parse_template()                        │            │
│  │          NO  ──► _registry_from_local_articles()         │            │
│  └─────────────────────────────────────────────────────────┘            │
│                                                                          │
│  parse_template() reads the markdown template, finds:                   │
│    - Section headings  (e.g. "1. Water Reform")                         │
│    - Jurisdiction sub-headings  (e.g. "New South Wales")                │
│    - URLs under each heading                                             │
│    - Example draft bodies (for few-shot LLM prompts)                    │
│                                                                          │
│  _registry_from_local_articles() reads each .txt file:                  │
│    - Filename stem → section + jurisdiction hints                        │
│      e.g.  Water_Reform__New_South_Wales__sources.txt                   │
│             └──────────┘  └─────────────┘                               │
│               section        jurisdiction                                │
│    - Each non-comment line → SourceEntry(source_type="url")             │
│                                                                          │
│  OUTPUT: data/source_registry.json                                      │
│  ┌─────────────────────────────────────────────────────────┐            │
│  │  {                                                       │            │
│  │    "sources": [                                          │            │
│  │      {                                                   │            │
│  │        "source_id": "water_reform__new_south___e4e4b3", │            │
│  │        "source_type": "url",                            │            │
│  │        "url": "https://waternsw.com.au/media-releases", │            │
│  │        "section": "Water Reform",                       │            │
│  │        "jurisdiction": "New South Wales",               │            │
│  │        "source_label": "Water_Reform__New_South_Wales"  │            │
│  │      }                                                   │            │
│  │    ],                                                    │            │
│  │    "examples": [ ... few-shot draft stubs ... ]         │            │
│  │  }                                                       │            │
│  └─────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 1 — Discovery

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 1 — Discovery → candidates table                                  │
│  Tools: httpx, feedparser, lxml, selectolax, markdown-it-py             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  INPUT: data/source_registry.json                                        │
│  CONFIG: lookback_days=21  (articles older than 21 days are dropped)    │
│                                                                          │
│  For each SourceEntry in the registry, branch by source_type:           │
│                                                                          │
│  source_type = "url"                                                     │
│  ┌───────────────────────────────────────────────────────┐              │
│  │  Step 1: Try RSS / Atom feed                          │              │
│  │    probe paths: /feed, /rss, /feed.xml, /atom.xml ... │              │
│  │    tool: feedparser                                   │              │
│  │    ├─ found  ──► extract entries, apply date cutoff   │              │
│  │    └─ not found ──► Step 2                            │              │
│  │                                                       │              │
│  │  Step 2: Try sitemap.xml                              │              │
│  │    url: /sitemap.xml, /sitemap_index.xml              │              │
│  │    tool: lxml (etree)                                 │              │
│  │    ├─ found  ──► extract <url><loc>, apply cutoff     │              │
│  │    └─ not found ──► Step 3                            │              │
│  │                                                       │              │
│  │  Step 3: HTML listing page                            │              │
│  │    tool: httpx (async) → selectolax                   │              │
│  │    extracts all <a href> links on the page            │              │
│  │    applies date cutoff if date found in link text     │              │
│  └───────────────────────────────────────────────────────┘              │
│                                                                          │
│  source_type = "html"  →  selectolax  (parse saved HTML)                │
│  source_type = "md"    →  markdown-it-py  (parse saved Markdown)        │
│                                                                          │
│  DATE FILTER (lookback_days):                                            │
│  ┌───────────────────────────────────────────────────────┐              │
│  │  cutoff = today - 21 days  (2026-04-02 for today)     │              │
│  │                                                       │              │
│  │  article.pub_date > cutoff  →  KEEP                   │              │
│  │  article.pub_date < cutoff  →  DROP (too old)         │              │
│  │  article.pub_date = None    →  KEEP + WARNING logged  │              │
│  │                             (never silently dropped)  │              │
│  └───────────────────────────────────────────────────────┘              │
│                                                                          │
│  OUTPUT: rows inserted into SQLite candidates table                      │
│  Each row has:  url, title, pub_date, section, jurisdiction,            │
│                 source_label, hash, status="new"                        │
│                                                                          │
│  EXAMPLE candidate row:                                                  │
│  ┌───────────────────────────────────────────────────────┐              │
│  │  id:           54                                     │              │
│  │  url:          https://waternsw.com.au/media/pricing  │              │
│  │  title:        "IPART pricing proposal 2025-30"       │              │
│  │  pub_date:     2026-04-10                             │              │
│  │  section:      Water Reform                           │              │
│  │  jurisdiction: New South Wales                        │              │
│  │  status:       new                                    │              │
│  └───────────────────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 3 — Deduplication

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 3 — Dedup → prune seen_hashes                                    │
│  Tools: SQLite, hashlib (SHA-256)                                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  hash = SHA-256( canonical_url + normalized_title )                      │
│                                                                          │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  hash in seen_hashes?                                │               │
│  │       YES  ──►  candidate.status = "filtered"        │               │
│  │                 (not re-processed)                   │               │
│  │       NO   ──►  INSERT into seen_hashes              │               │
│  │                 candidate continues downstream       │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  TTL prune:  hashes older than (lookback_days × 4 = 84 days)            │
│              are deleted so old URLs can re-surface if updated           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 4 — Fetch Full Content

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 4 — Fetch → context_bundles table                                │
│  Tools: httpx (async), Playwright, trafilatura, pdfplumber               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  For each candidate with status="new":                                   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  1. Fetch article page                               │               │
│  │     httpx (fast) ──► try first                       │               │
│  │     Playwright   ──► fallback if JS-rendered         │               │
│  │                                                      │               │
│  │  2. Clean boilerplate                                │               │
│  │     trafilatura strips nav, ads, footers             │               │
│  │     → clean main_text                                │               │
│  │                                                      │               │
│  │  3. Discover sublinks (depth=1)                      │               │
│  │     extract links matching sublink_patterns from     │               │
│  │     source_registry entry (e.g. /media-releases/,   │               │
│  │     \.pdf$)                                          │               │
│  │     fetch each sublink → extract text                │               │
│  │                                                      │               │
│  │  4. Cache to disk                                    │               │
│  │     data/cache/{sha256}.html  /  .pdf  /  .txt       │               │
│  │     (avoids re-fetching on pipeline re-run)          │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  OUTPUT: context_bundles table row per candidate                         │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  candidate_id:  54                                   │               │
│  │  main_text:     "WaterNSW has submitted its 2025-30  │               │
│  │                  pricing proposal to IPART..."       │               │
│  │  sublinks_json: [{"url": "...", "text": "..."}]      │               │
│  │  pdfs_json:     [{"url": "...", "text": "..."}]      │               │
│  │  metadata_json: {"fetched_at": "...", "bytes": 4821} │               │
│  └──────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 5 — Relevance Filter (LLM)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 5 — Relevance Filter                                              │
│  LLM: gpt-4o-mini  (cheap model — binary gate, no need for quality)     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  PROMPT SENT TO LLM (relevance_v1.j2):                                  │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  "You are a regulatory analyst for the water sector. │               │
│  │   Is the following article relevant to water         │               │
│  │   regulation, water supply, water rights, water      │               │
│  │   quality, or water management in Australia?         │               │
│  │                                                      │               │
│  │   Title: IPART pricing proposal 2025-30              │               │
│  │   Text:  WaterNSW has submitted...                   │               │
│  │                                                      │               │
│  │   Return JSON: {relevant: bool, reason: str}"        │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  RESULT HANDLING:                                                        │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  relevant = true   ──► candidate.status = "fetched"  │               │
│  │                        continues to Stage 6          │               │
│  │                                                      │               │
│  │  relevant = false  ──► candidate.status = "filtered" │               │
│  │                        reason logged to audit_log    │               │
│  │                        NOT passed downstream         │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  EXAMPLE FILTERED OUT:                                                   │
│  Title: "Fun activities for kids" — reason: "Education content,          │
│          not water regulation or policy"  → status = filtered           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 6 — Classification (LLM)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 6 — Classification (section + jurisdiction + content_type)       │
│  LLM: gpt-4o  (quality model — needs nuanced understanding)             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  WHAT IS CLASSIFIED:                                                     │
│                                                                          │
│  ① SECTION  — which of the 9 topics does this article belong to?        │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  1.  Water Reform                                    │               │
│  │  2.  Urban Water Supply and Use                      │               │
│  │  3.  Rural Water Supply and Use                      │               │
│  │  4.  Wastewater and Water Reuse                      │               │
│  │  5.  Collection                                      │               │
│  │  6.  Conservation                                    │               │
│  │  7.  Water Rights                                    │               │
│  │  8.  Waterways and Flood Mitigation                  │               │
│  │  9.  General Water Quality and Sustainability        │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ② JURISDICTION  — which state/territory does this relate to?           │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  National · Victoria · New South Wales · Queensland  │               │
│  │  Western Australia · South Australia · Tasmania      │               │
│  │  Australian Capital Territory · Northern Territory   │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ③ CONTENT TYPE  — which of 6 draft templates should be used?           │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  new_plan      → new legislation / plan made         │               │
│  │  media_release → agency press release with quote     │               │
│  │  weekly_report → recurring agency report             │               │
│  │  bullet_list   → multiple releases bundled together  │               │
│  │  simple_update → short advisory / update notice      │               │
│  │  gazette_bundle→ government gazette notices          │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  CONFIDENCE THRESHOLD:                                                   │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  confidence >= 0.7  ──► auto-classified              │               │
│  │                         continues to Stage 7         │               │
│  │  confidence <  0.7  ──► editor queue                 │               │
│  │                         human reviews classification │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  EXAMPLE CLASSIFICATION:                                                 │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Input:   "WaterNSW IPART pricing proposal 2025-30"  │               │
│  │  Output:  {                                          │               │
│  │    section:      "Water Reform",                     │               │
│  │    jurisdiction: "New South Wales",                  │               │
│  │    content_type: "media_release",                    │               │
│  │    confidence:   0.92                                │               │
│  │  }                                                   │               │
│  └──────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 7 — Draft Generation (LLM)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 7 — Draft Generation                                              │
│  Tools: Jinja2 templates, gpt-4o                                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Based on content_type, one of 6 Jinja2 templates is selected           │
│  and the LLM fills the slots using ONLY facts from context_bundle       │
│                                                                          │
│  TEMPLATE SELECTION + EXAMPLE OUTPUT:                                   │
│                                                                          │
│  ┌─ content_type = "media_release" ─────────────────────────────────┐  │
│  │  Template (media_release.j2):                                     │  │
│  │    **{Title}**                                                    │  │
│  │    {Agency} has announced that "[{quote_1}]".                     │  │
│  │    According to {Agency}, "[{quote_2}]".                          │  │
│  │    {Agency}'s media release ({date})                              │  │
│  │    *(Source: {Agency})*                                           │  │
│  │                                                                   │  │
│  │  Filled output:                                                   │  │
│  │    **WaterNSW Submits Pricing Proposal to IPART**                 │  │
│  │    WaterNSW has announced that "[we are seeking a fair            │  │
│  │    return to fund critical infrastructure]".                      │  │
│  │    According to WaterNSW, "[customers will see a 3.2%             │  │
│  │    annual increase from 2025]".                                   │  │
│  │    WaterNSW's media release (14 April 2026)                       │  │
│  │    *(Source: WaterNSW)*                                           │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─ content_type = "new_plan" ───────────────────────────────────────┐  │
│  │  **{Title}**                                                      │  │
│  │  The {plan_name} has been made under {parent_act}.                │  │
│  │  The Plan:                                                        │  │
│  │  - {bullet_1};                                                    │  │
│  │  - {bullet_2}; and                                                │  │
│  │  - {bullet_3}.                                                    │  │
│  │  The Plan commenced on {date}.                                    │  │
│  │  *(Source: Lawlex)*                                               │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─ content_type = "gazette_bundle" ─────────────────────────────────┐  │
│  │  **Gazette Notices**                                              │  │
│  │  Various notices in Government Gazette No. {number} ({date}),    │  │
│  │  pp. {pages}, under the {act}, regarding the:                    │  │
│  │  - {notice_1};                                                    │  │
│  │  - {notice_2}; and                                                │  │
│  │  - {notice_3}.                                                    │  │
│  │  *(Source: {gazette}; Lawlex)*                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ANTI-HALLUCINATION RULE applied at prompt level:                        │
│  "Use ONLY facts from the context below. Every quote must appear         │
│   verbatim in the source text. Do not invent dates, act numbers,         │
│   or agency names."                                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 8 — Hallucination Guard + Validation

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 8 — Hallucination Guard (pure Python — no LLM)                   │
│  Tools: str.find(), dateparser, regex, Pydantic                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  5 CHECKS (all must pass):                                               │
│                                                                          │
│  ① QUOTE VERBATIM CHECK                                                 │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Every "[quoted phrase]" in draft_text               │               │
│  │  must appear as an exact substring in               │               │
│  │  context_bundle.main_text                            │               │
│  │                                                      │               │
│  │  PASS: "fair return to fund critical infrastructure" │               │
│  │        found in source HTML ✓                        │               │
│  │  FAIL: "improve water access for all Australians"   │               │
│  │        NOT found in source → hallucination detected  │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ② DATE VERBATIM CHECK                                                  │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Every date in draft_text must appear in source text │               │
│  │  FAIL example: draft says "12 March 2026" but source │               │
│  │  only mentions "March 2026" → rejected               │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ③ AGENCY WHITELIST CHECK                                               │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Agency name in draft must be in config.yaml list    │               │
│  │  for that jurisdiction:                              │               │
│  │                                                      │               │
│  │  jurisdiction = "New South Wales"                    │               │
│  │  allowed = [DPIE, DPE, Hunter Water, IPART,          │               │
│  │             Murrumbidgee Irrigation, Sydney Water,   │               │
│  │             WaterNSW, ...]                           │               │
│  │                                                      │               │
│  │  PASS: "WaterNSW" → in whitelist ✓                   │               │
│  │  FAIL: "WaterAuthority NSW" → NOT in whitelist       │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ④ SECTION / JURISDICTION ENUM CHECK                                    │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  section ∈ {9 valid sections from config.yaml}       │               │
│  │  jurisdiction ∈ {9 valid jurisdictions}              │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  ⑤ SOURCE URL REACHABILITY                                              │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  HTTP HEAD request to source_url                     │               │
│  │  status 200/301/302 → PASS                           │               │
│  │  status 404/5xx     → FAIL                           │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  OUTCOME:                                                                │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  All 5 pass  ──► draft.status = "validated"          │               │
│  │  Any fail    ──► draft.status = "review"             │               │
│  │                  validation_errors_json stored        │               │
│  │                  human must review in Stage 10 UI    │               │
│  └──────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 9 — Assembly

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 9 — Assemble → horizon_payload.json                              │
│  Tools: Pydantic, json                                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Groups all validated drafts by SECTION → then by JURISDICTION          │
│  in a fixed canonical order defined in config.yaml                      │
│                                                                          │
│  SECTION ORDER (fixed, always 1–9):                                     │
│  1 → Water Reform                                                        │
│  2 → Urban Water Supply and Use                                          │
│  3 → Rural Water Supply and Use                                          │
│  4 → Wastewater and Water Reuse                                          │
│  5 → Collection                                                          │
│  6 → Conservation                                                        │
│  7 → Water Rights                                                        │
│  8 → Waterways and Flood Mitigation                                      │
│  9 → General Water Quality and Sustainability                            │
│                                                                          │
│  JURISDICTION ORDER within each section (fixed):                         │
│  National → Victoria → New South Wales → Queensland →                   │
│  Western Australia → South Australia → Tasmania →                       │
│  Australian Capital Territory → Northern Territory                       │
│                                                                          │
│  EXAMPLE assembled structure:                                            │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Section: Water Reform                               │               │
│  │    ├── New South Wales                               │               │
│  │    │     • WaterNSW IPART pricing proposal (draft)   │               │
│  │    │     • Water licensing fees update (draft)       │               │
│  │    └── Victoria                                      │               │
│  │          • Melbourne Water recycled water plan       │               │
│  │                                                      │               │
│  │  Section: Water Rights                               │               │
│  │    └── National                                      │               │
│  │          • MDBA water trading policy update          │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  Empty section+jurisdiction combos are OMITTED (not rendered as blanks)  │
│                                                                          │
│  Appends boilerplate:                                                    │
│    - Upcoming Dates section                                              │
│    - Previous Newsfeeds section                                          │
│    - Copyright footer                                                    │
│                                                                          │
│  OUTPUT: outputs/2026-04-23/horizon_payload.json (AssembledNewsfeed)    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### STAGE 11 — Render

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 11 — Render → .docx + .md                                        │
│  Tools: python-docx, OxmlElement (OOXML), markdown string builder       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  INPUT: outputs/2026-04-23/horizon_payload.json                         │
│                                                                          │
│  .docx structure built:                                                  │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  Title heading:   "Water Newsfeed"          (H0)     │               │
│  │  Issue date       italic paragraph                   │               │
│  │  ─────────────────────────────────────               │               │
│  │  Index heading    (H2)                               │               │
│  │    1. Water Reform                                   │               │
│  │    2. Urban Water Supply and Use                     │               │
│  │    ...                                               │               │
│  │    N. Upcoming Dates                                 │               │
│  │  ─────────────────────────────────────               │               │
│  │  Legislation Hotline boilerplate                     │               │
│  │  ─────────────────────────────────────               │               │
│  │  1. Water Reform            (H1)                     │               │
│  │    New South Wales – Water Reform  (H2)              │               │
│  │      **WaterNSW IPART Pricing Proposal**  ← bold     │               │
│  │      WaterNSW has announced...            ← body     │               │
│  │      *(Source: WaterNSW)*                ← italic    │               │
│  │      • bullet item 1                     ← list      │               │
│  │      • bullet item 2                                  │               │
│  │  ─────────────────────────────────────               │               │
│  │  Upcoming Dates             (H1)                     │               │
│  │  Previous Water Newsfeeds   (H1)                     │               │
│  │  Copyright footer           italic                   │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  Draft text line-by-line parser:                                         │
│  ┌──────────────────────────────────────────────────────┐               │
│  │  **Title**        → bold run paragraph               │               │
│  │  *(Source: X)*    → italic run paragraph             │               │
│  │  - bullet item    → List Bullet style paragraph      │               │
│  │  plain text       → Normal paragraph                 │               │
│  └──────────────────────────────────────────────────────┘               │
│                                                                          │
│  PermissionError fallback:                                               │
│  If Water_Newsfeed.docx is open in Word → writes                        │
│  Water_Newsfeed_143022.docx (timestamped)                               │
│                                                                          │
│  OUTPUT:                                                                 │
│    outputs/2026-04-23/Water_Newsfeed.docx                               │
│    outputs/2026-04-23/Water_Newsfeed.md                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## PART 3 — SECTIONS × JURISDICTIONS × AGENCIES MATRIX

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  HOW SECTION + JURISDICTION + AGENCY WORK TOGETHER                                    │
│                                                                                        │
│  SECTION        = WHAT topic the article covers  (9 topics, from config.yaml)         │
│  JURISDICTION   = WHERE in Australia  (9 states/territories + National)               │
│  AGENCY         = WHO published it  (whitelist per jurisdiction in config.yaml)       │
│                                                                                        │
│  Each draft entry lands in exactly ONE cell of this grid:                              │
│                                                                                        │
│                 Nat   VIC    NSW    QLD    WA     SA     TAS    ACT    NT              │
│               ┌─────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐        │
│  Water Reform │     │      │  ✓   │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Urban Water  │     │  ✓   │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Rural Water  │     │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Wastewater   │     │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Collection   │     │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Conservation │     │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Water Rights │  ✓  │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  Waterways    │     │      │      │      │      │      │      │      │      │        │
│               ├─────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤        │
│  General WQ   │     │      │      │      │      │      │      │      │      │        │
│               └─────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘        │
│                                                                                        │
│  Only filled cells appear in the final .docx — empty cells are omitted               │
└──────────────────────────────────────────────────────────────────────────────────────┘


AGENCY WHITELIST BY JURISDICTION (from config.yaml)
────────────────────────────────────────────────────────────────────────────────────────
National           │ Victoria                │ New South Wales
───────────────────┼─────────────────────────┼──────────────────────────────────
ABARES             │ Barwon Water            │ DPIE / DPE
ACCC               │ City West Water         │ Hunter Water
BoM                │ DEECA / DELWP           │ IPART
DAWE               │ Melbourne Water         │ Murrumbidgee Irrigation
DCCEEW             │ South East Water        │ NSW Dept Primary Industries
MDBA               │ Yarra Valley Water      │ Sydney Water
National Water     │ GMW, GWMWater, SRW      │ WaterNSW
Grid Authority     │ VEWH, ESC, EWOV ...     │
AWA                │                         │

Queensland         │ Western Australia       │ South Australia
───────────────────┼─────────────────────────┼──────────────────────────────────
DNRM               │ DER / DWER              │ DEW
Seqwater           │ Water Corporation       │ ESCOSA
Sunwater           │                         │ SA Water
Urban Utilities    │                         │ WaterConnect

Tasmania           │ ACT                     │ Northern Territory
───────────────────┼─────────────────────────┼──────────────────────────────────
DPIPWE             │ Icon Water              │ DENR
Economic Regulator │                         │ PowerWater
Tasmanian          │                         │ Utilicom
Irrigation         │                         │
TasWater           │                         │
```

---

## PART 4 — CONTENT TYPES WITH FULL EXAMPLES

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│  6 CONTENT TYPES — Templates + Examples                                              │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                       │
│  TYPE 1: new_plan  (new legislation, plan, or statutory instrument)                  │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **Water Management Amendment (Metering) Plan 2026**                           │  │
│  │  The Water Management Amendment (Metering) Plan 2026 (NSW) has been made       │  │
│  │  under the authority of the Water Management Act 2000.                         │  │
│  │  The Plan:                                                                     │  │
│  │  - requires telemetry on all water access licences above 100 ML/year;         │  │
│  │  - introduces civil penalties for non-compliance; and                          │  │
│  │  - sets a transition period of 12 months from commencement.                   │  │
│  │  The Plan commenced on 1 April 2026.                                           │  │
│  │  *(Source: Lawlex Legislative Alert & Premium Research)*                       │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                       │
│  TYPE 2: media_release  (agency press release, usually has a quote)                  │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **WaterNSW Submits 2025-30 Pricing Proposal to IPART**                        │  │
│  │  WaterNSW has announced that "[the proposal seeks to fund $2.3 billion in      │  │
│  │  essential infrastructure over the next five years]".                          │  │
│  │  According to WaterNSW, "[customers in Greater Sydney will see increases        │  │
│  │  of 3.2% per year to fund drought resilience projects]".                       │  │
│  │  WaterNSW's media release (14 April 2026)                                      │  │
│  │  *(Source: WaterNSW)*                                                          │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                       │
│  TYPE 3: weekly_report  (recurring agency report / monitoring data)                  │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **WaterNSW Weekly Storage Report**                                            │  │
│  │  WaterNSW has made available its latest Weekly Storage Report for the week     │  │
│  │  ending 20 April 2026, which provides information on storage levels for        │  │
│  │  Greater Sydney and Regional NSW dams.                                         │  │
│  │  *(Source: WaterNSW)*                                                          │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                       │
│  TYPE 4: bullet_list  (multiple releases bundled together)                           │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **IPART Media Releases**                                                      │  │
│  │  IPART has made available the following media releases:                        │  │
│  │  - Review of Sydney Water prices 2025-30 (8 April 2026);                      │  │
│  │  - Hunter Water determination final report (15 April 2026); and               │  │
│  │  - Water NSW annual pricing review (21 April 2026).                            │  │
│  │  *(Source: IPART)*                                                             │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                       │
│  TYPE 5: simple_update  (short advisory, notice, or update)                          │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **WaterNSW Water Quality Advisory — Warragamba Catchment**                    │  │
│  │  WaterNSW has made available an update - Water Quality Advisory (10 April      │  │
│  │  2026), which advises that "[elevated turbidity levels have been detected       │  │
│  │  following recent rainfall events in the Warragamba catchment]".               │  │
│  │  *(Source: WaterNSW)*                                                          │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                       │
│  TYPE 6: gazette_bundle  (Government Gazette notices under an Act)                   │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  **Gazette Notices**                                                           │  │
│  │  Various notices have been issued in Government Gazette No. 42 (18 April       │  │
│  │  2026), pp. 1204–1211, under the Water Management Act 2000, regarding the:    │  │
│  │  - amendment of Water Access Licence WAL 123456 (Murrumbidgee Valley);        │  │
│  │  - suspension of Approval No. 88712 pending compliance review; and            │  │
│  │  - cancellation of Flood Work Approval FW-2019-003.                           │  │
│  │  *(Source: NSW Government Gazette; Lawlex Legislative Alert)*                  │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## PART 5 — DATA FLOW THROUGH SQLITE

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  SQLite TABLES AND HOW DATA MOVES BETWEEN THEM                               │
│                                                                               │
│  candidates                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │ id │ url          │ title       │ section      │ jurisdiction │ status│    │
│  │ 54 │ waternsw.com │ IPART 25-30 │ Water Reform │ New South W. │ new   │    │
│  └───────────────────────────────────┬─────────────────────────────────┘     │
│                                      │ Stage 4 fetch                          │
│                                      ▼                                        │
│  context_bundles                                                              │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │ candidate_id=54 │ main_text="WaterNSW has submitted its 2025-30... │      │
│  │                  sublinks_json=[...]  pdfs_json=[...]              │      │
│  └───────────────────────────────────┬────────────────────────────────┘      │
│                                      │ Stages 5,6,7 use main_text            │
│                                      ▼                                        │
│  drafts                                                                       │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │ candidate_id=54 │ content_type=media_release                       │      │
│  │ section=Water Reform │ jurisdiction=New South Wales                │      │
│  │ confidence=0.92 │ draft_text="**WaterNSW Submits..."              │      │
│  │ editor_decision=pending                                            │      │
│  └───────────────────────────────────┬────────────────────────────────┘      │
│                                      │ Stage 9 assembly                       │
│                                      ▼                                        │
│  STATUS LIFECYCLE:                                                            │
│  new → fetched → relevant → classified → drafted → validated → review        │
│                                                        │           │          │
│                                               Stage 10 UI approves/rejects   │
│                                                        ▼           ▼          │
│                                                   approved     rejected       │
│                                                        │                      │
│                                                        ▼                      │
│                                                   published                   │
│                                                (after Stage 12)               │
│                                                                               │
│  audit_log (every LLM call recorded)                                         │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │ stage=5 │ candidate_id=54 │ model=gpt-4o-mini │ provider=openai   │      │
│  │ input_tokens=842 │ output_tokens=38 │ latency_ms=1240             │      │
│  │ fallback_triggered=0 │ prompt_version=relevance_v1                │      │
│  └────────────────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## PART 6 — LLM CALL STRATEGY

```
┌──────────────────────────────────────────────────────────────────────────┐
│  LLM PROVIDER STRATEGY                                                    │
│                                                                           │
│  Primary:  OpenAI   (config: llm.primary = openai)                       │
│  Fallback: Bedrock  (config: llm.fallback = bedrock)                     │
│                                                                           │
│  Model tier selection:                                                    │
│  ┌────────────────────┬─────────────────────┬──────────────────────┐     │
│  │  Stage             │  Tier               │  Model               │     │
│  ├────────────────────┼─────────────────────┼──────────────────────┤     │
│  │  Stage 5 Relevance │  cheap              │  gpt-4o-mini         │     │
│  │  Stage 6 Classify  │  quality            │  gpt-4o              │     │
│  │  Stage 7 Draft     │  quality            │  gpt-4o              │     │
│  └────────────────────┴─────────────────────┴──────────────────────┘     │
│                                                                           │
│  Failover logic (in llm_client.py):                                      │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  call OpenAI  ──► ThrottlingException / Timeout / JSON error      │   │
│  │                        ──► fallback to Bedrock (same tier)        │   │
│  │                        ──► if Bedrock also fails → raise          │   │
│  │                            LLMStructuredOutputError               │   │
│  │                            item goes to editor queue              │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  Structured output:                                                       │
│  OpenAI  → response_format={"type":"json_schema", "json_schema": ...}    │
│  Bedrock → Converse API with tool_use forcing a schema-shaped tool call  │
└──────────────────────────────────────────────────────────────────────────┘
```
