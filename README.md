# prospecting-pipeline

Automatically scrapes researcher information from university websites, enriches it with Google Scholar publication data, and filters it by relevance to a situation of interest — all synced to a Google Sheet.

## What it does

**Pipeline 1 — Institute scraping (run periodically)**
1. Discovers faculty/department pages for configured universities via SerpAPI web search
2. Extracts PI names, roles, and departments using Claude Sonnet (batch LLM)
3. Finds each PI's lab homepage (SerpAPI + HEAD-checked URL patterns + Haiku/Sonnet summaries)
4. Locates the lab members page (heuristic regex → Haiku fallback)
5. Extracts all lab members and roles (Haiku/Sonnet batch extraction)
6. Syncs results incrementally to the *Master List* Google Sheet in groups of ~15 PIs — adding new rows, updating changed fields, reactivating returning members, and marking departed members `inactive` (scoped to institutions in the current run only). Supports checkpointing for crash recovery.

**Pipeline 2 — Situation filtering (run on-demand)**
1. Coarse-filters **all** active PIs across all institutions by relevance to a situation of interest (Haiku YES/NO per lab)
2. Looks up recent publications for each member via Google Scholar (SerpAPI Author endpoint)
3. Fine-scores individual researchers against the situation (Sonnet with lab context + papers)
4. Finds contact emails via a three-method cascade: lab members page → web search → institutional pattern guess
5. Writes relevance scores for **all** active rows — researchers whose PI passed the coarse filter get actual scores; researchers whose PI did not pass are marked not relevant with an explanation. Every active row gets a current relevance assessment after each Pipeline 2 run.

## Prerequisites

- Python 3.11+
- A Google Cloud project with the **Sheets API** enabled and a **service account** JSON key
- API keys for **Anthropic**, **SerpAPI**, and the Google Sheet ID

## Setup

```bash
# 1. Clone and enter the repo
git clone https://github.com/dorozcosio/prospecting-pipeline.git
cd prospecting-pipeline

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure secrets
cp .env.example .env
# Edit .env and fill in all four values (see .env.example for descriptions)

# 5. Verify credentials
python scripts/verify_sheets.py
python scripts/verify_anthropic.py
python scripts/verify_serpapi.py
```

## Usage

### Pipeline 1 — Scrape and sync

```bash
# Run for all configured institutions
python -m src.run_pipeline1

# Run for a single institution only (matches name in config/institutions.yaml)
python -m src.run_pipeline1 --institution MIT

# Resume from a checkpoint after a crash
python -m src.run_pipeline1 --resume

# Clear checkpoint and start over
python -m src.run_pipeline1 --fresh

# Dry run: execute all steps but save rows to JSON instead of writing to the sheet
python -m src.run_pipeline1 --dry-run
python -m src.run_pipeline1 --dry-run --institution MIT
```

### Pipeline 2 — Filter by situation

```bash
# Score all researchers for relevance to a situation
python -m src.run_pipeline2 --situation "CRISPR gene editing for cancer therapy"

# Dry run: score everything but don't write back to the sheet
python -m src.run_pipeline2 --situation "..." --dry-run

# Skip Scholar lookups (fast re-scoring when papers are already populated)
python -m src.run_pipeline2 --situation "..." --skip-scholar

# Combine flags
python -m src.run_pipeline2 --situation "..." --skip-scholar --dry-run
```

### CLI flags reference

| Flag | Pipelines | Description |
|------|-----------|-------------|
| `--dry-run` | P1, P2 | Skip the sheet write; save output to `logs/p{1,2}_dry_run_{ts}.json` |
| `--institution NAME` | P1 | Limit to one institution (name must match `config/institutions.yaml`) |
| `--resume` | P1 | Continue from last checkpoint (skips already-completed PIs) |
| `--fresh` | P1 | Clear checkpoint and start from scratch |
| `--group-size N` | P1 | PIs per incremental write batch (default: 15) |
| `--situation TEXT` | P2 | **(Required)** Free-text description of the situation of interest |
| `--skip-scholar` | P2 | Skip Google Scholar lookups (step 2) entirely |

## Architecture

```
src/
├── config.py                  # Loads .env + YAML configs into a Config dataclass
├── sheets.py                  # Google Sheets client (read/append/update/batch_update)
├── llm.py                     # Anthropic wrapper (call_haiku, call_sonnet) with pacing + retry + JSONL logging
├── search.py                  # SerpAPI wrappers (web_search, scholar_search)
├── cache.py                   # SQLite page cache (get/set with TTL)
├── checkpoint.py              # File-based checkpointing for resumable P1 runs
├── logging_config.py          # JSON file log + human-readable console log
├── run_pipeline1.py           # Pipeline 1 CLI entry point
├── run_pipeline2.py           # Pipeline 2 CLI entry point
├── pipeline1/
│   ├── step1_url_discovery.py     # Discover faculty page URLs via SerpAPI + heuristic scoring
│   ├── step2_pi_extraction.py     # Extract PI names/roles from faculty pages (Sonnet batch)
│   ├── step3_lab_homepages.py     # Find lab homepages + research summaries (SerpAPI + Haiku/Sonnet)
│   ├── step4_member_pages.py      # Find the lab members page per PI (heuristic + Haiku)
│   ├── step5_member_extraction.py # Extract member names/roles from members pages (Haiku/Sonnet batch)
│   └── step6_sheet_writer.py      # Sync member rows to the Master List sheet (incremental, scoped)
└── pipeline2/
    ├── step1_coarse_filter.py     # YES/NO relevance filter per lab across all institutions (Haiku batch)
    ├── step2_scholar_lookup.py    # Google Scholar author lookup via SerpAPI
    ├── step3_fine_scoring.py      # Per-researcher relevance scoring with reasoning (Sonnet)
    ├── step4_email_lookup.py      # Email cascade: members page → web search → pattern guess
    └── step5_sheet_writer.py      # Write P2 enrichment data back to the sheet (all active rows)

config/
├── institutions.yaml          # Institution names and domains to scrape
└── settings.yaml              # Batch sizes, Scholar cache TTL, delay ranges

logs/
├── run.log                    # Structured JSON log of every run
├── llm_calls.jsonl            # Per-call log of every Anthropic request/response
├── p1_checkpoint.json         # Checkpoint file for resumable P1 runs
└── p{1,2}_dry_run_*.json      # Dry-run output files
```

## Design decisions

**Status vs. relevance** — The `status` column tracks data freshness ("does this
person still appear on their institution's website?"), set only by Pipeline 1. The
`relevance_flag` column tracks topical fit ("does this person's work match the
current situation?"), set by Pipeline 2. These are intentionally separate — Pipeline 1
can run for one institution without affecting others, and Pipeline 2 re-mines all
cached data on every run.

**Scoped deactivation** — Pipeline 1 only deactivates rows from institutions it
actually scraped. Running `--institution MIT` will never touch Stanford or Harvard rows.
The `institution_last_scraped` column records when each institution was last scraped,
regardless of whether individual rows changed.

**Full-list scoring** — Pipeline 2 scores all active rows across all institutions,
not just those recently scraped. Cached lab summaries and paper titles from previous
runs are valuable when the situation of interest changes.

**Haiku-first model selection** — Steps 3 (lab summaries) and 5 (member extraction)
default to Haiku for short/structured pages, escalating to Sonnet only for long or
complex pages. This conserves Sonnet's rate budget.

**Incremental writes + checkpointing** — Pipeline 1 writes to the sheet after each
group of ~15 PIs and saves a checkpoint. If the run crashes, `--resume` picks up
where it left off.

**Rate limit pacing** — Minimum delays between calls to the same model (13 s for
Free Tier's 5 RPM). Sonnet and Haiku have independent rate pools and don't block
each other.

## Cost notes

| Step | Service | Approximate cost |
|------|---------|-----------------|
| P1 Step 1 — URL discovery | SerpAPI | ~$0.01 per institution (3 searches) |
| P1 Step 2 — PI extraction | Anthropic Sonnet | ~$0.01–0.05 per page batch |
| P1 Step 3 — Lab homepages | SerpAPI + Haiku / Sonnet | ~$0.02 per PI |
| P1 Step 5 — Member extraction | Anthropic Haiku / Sonnet | ~$0.01–0.03 per lab batch |
| P2 Step 1 — Coarse filter | Anthropic Haiku | ~$0.001 per batch of 30 PIs |
| P2 Step 2 — Scholar lookup | SerpAPI | ~$0.02 per researcher (2 API calls) + random delay |
| P2 Step 3 — Fine scoring | Anthropic Sonnet | ~$0.01–0.05 per batch of 7 researchers |
| P2 Step 4 — Email lookup | SerpAPI + Haiku | ~$0.01 per researcher (web search + optional Haiku) |

Typical full P1 run across 3 institutions with ~100 PIs: **~$2–5**.
Typical P2 run with 50 passing PIs: **~$1–3** (less with `--skip-scholar`).

Google Sheets API: free within quota (no cost for normal usage).

## Configuration

**`config/institutions.yaml`** — Add institutions here:
```yaml
institutions:
  - name: "MIT"
    domain: "mit.edu"
  - name: "Stanford University"
    domain: "stanford.edu"
```

**`config/settings.yaml`** — Tune batch sizes and Scholar behaviour:
```yaml
scholar_cache_days: 90        # Skip re-lookup if enriched within this many days
haiku_batch_size: 40          # Items per Haiku batch call
sonnet_batch_size: 15         # Items per Sonnet batch call
scholar_delay_range: [10, 30] # Randomised sleep between Scholar requests (seconds)
scholar_backend: serpapi      # Only 'serpapi' is currently implemented
sonnet_call_delay: 13         # Min seconds between Sonnet calls (Free Tier: 5 RPM)
haiku_call_delay: 13          # Min seconds between Haiku calls (Free Tier: 5 RPM)
pi_group_size: 15             # PIs per incremental write group (P1 Phase 2)
```

## Branches

- `main` — Stable releases. Tagged with version numbers.
- `develop` — Default working branch. Create feature branches off here.
