# prospecting-pipeline

Scrapes researcher information from university websites, stores it in Google Sheets, and filters/enriches it using LLM calls and Google Scholar lookups.

## What it does

1. **Scrape** — Crawls university faculty pages to extract researcher names, titles, emails, and research areas
2. **Store** — Writes structured data into a Google Sheet ("Prospecting Pipeline — Master List")
3. **Enrich** — Uses Google Scholar (via SerpAPI) to pull publication counts, h-index, and recent papers
4. **Filter** — Runs LLM-based relevance scoring (Claude Haiku/Sonnet) to rank researchers by fit

## Services used

| Service | Purpose |
|---|---|
| GitHub | Version control |
| Google Sheets API | Primary data store for researcher records |
| Anthropic API (Claude) | LLM-based filtering and enrichment |
| SerpAPI | Google Scholar lookups and web search |

## Setup

### Prerequisites
- Python 3.12+
- A Google Cloud project with Sheets API enabled
- Service account JSON key (see `.env.example`)

### Install
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure
Copy `.env.example` to `.env` and fill in all values:
```bash
cp .env.example .env
```

### Verify credentials
```bash
python scripts/verify_sheets.py
python scripts/verify_anthropic.py
python scripts/verify_serpapi.py
```

## Branches

- `main` — Protected. Requires PR review before merging.
- `develop` — Default working branch. Create feature branches off here.
