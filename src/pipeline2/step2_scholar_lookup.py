"""
Pipeline 2, Step 2: Google Scholar lookup for lab members.

Adapter pattern:
  ScholarBackend (Protocol)  — defines the lookup interface
  SerpAPIScholarBackend      — production implementation via SerpAPI
  ScholarlyBackend           — stub for future `scholarly` library backend

Orchestrator:
  lookup_members(candidate_pis, config, tab_name) -> dict[(pi_name, member_name), ScholarResult]

Reliability notes:
  - The SerpAPI Google Scholar Profiles endpoint is discontinued; this
    implementation instead uses the standard google_scholar paper-search endpoint
    (query: author:"Name" Institution) and extracts author IDs from the
    publication_info.authors[].link fields, then calls google_scholar_author for
    their full article list.
  - Google Scholar aggressively rate-limits scrapers. Randomised delays between
    requests reduce but do not eliminate the risk of temporary bans.
  - Disambiguation is best-effort: common names may still yield ambiguous results
    even with an affiliation hint.
  - If the error rate for a run exceeds 20%, the orchestrator pauses and logs a
    prominent warning — check API quota or consider switching backends.
  - scholar_cache_days (default 90) prevents redundant re-lookups for rows that
    were already enriched recently.
"""
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from serpapi import GoogleSearch

from src.config import Config
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

_MAX_PAPERS = 6
_ERROR_RATE_THRESHOLD = 0.20
_ERROR_PAUSE_SECONDS = 60

# Regex to extract author_id from a Google Scholar profile URL
# e.g. https://scholar.google.com/citations?user=rDfyQnIAAAAJ&…
_AUTHOR_ID_RE = re.compile(r"[?&]user=([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class ScholarResult:
    status: str                          # "found" | "ambiguous" | "not_found" | "error"
    papers: list[str] = field(default_factory=list)  # up to _MAX_PAPERS titles
    profile_url: str = ""               # empty if not found
    error_message: str = ""            # empty if no error


# ---------------------------------------------------------------------------
# Backend protocol (interface)
# ---------------------------------------------------------------------------

class ScholarBackend(Protocol):
    def lookup(self, name: str, institution: str = "") -> ScholarResult: ...


# ---------------------------------------------------------------------------
# SerpAPI implementation
# ---------------------------------------------------------------------------

class SerpAPIScholarBackend:
    """
    Scholar lookup via SerpAPI.

    Flow per researcher:
      1. Search google_scholar with 'author:"Name" Institution' to surface papers.
      2. Scan publication_info.authors[].link fields for 'user=<author_id>' params.
      3. Collect unique author_ids whose displayed name contains the target's last name.
         - 1 unique id  → status "found"
         - >1 unique id → status "ambiguous" (pick the most-frequent id)
         - 0 ids, but papers exist → status "ambiguous" (Scholar may abbreviate names)
         - no papers at all      → status "not_found"
      4. Fetch up to _MAX_PAPERS titles from google_scholar_author using the author_id.
      5. Apply a randomised delay to respect Scholar's rate limits.
    """

    def __init__(self, config: Config) -> None:
        self._api_key = config.serpapi_api_key
        self._delay_range = config.settings.scholar_delay_range

    def lookup(self, name: str, institution: str = "") -> ScholarResult:
        self._sleep()
        try:
            return self._do_lookup(name, institution)
        except Exception as exc:
            logger.error("SerpAPI scholar error for %r: %s", name, exc)
            return ScholarResult(status="error", error_message=str(exc))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sleep(self) -> None:
        wait = random.uniform(self._delay_range[0], self._delay_range[1])
        logger.debug("Scholar lookup — sleeping %.1fs before next request", wait)
        time.sleep(wait)

    def _do_lookup(self, name: str, institution: str) -> ScholarResult:
        query = f'author:"{name}"'
        if institution:
            query += f" {institution}"

        params = {
            "engine": "google_scholar",
            "q": query,
            "num": 10,
            "api_key": self._api_key,
        }
        results = GoogleSearch(params).get_dict()
        organic = results.get("organic_results", [])

        if not organic:
            return ScholarResult(status="not_found")

        # Extract all author_ids whose name plausibly matches the queried person
        last_name = name.lower().split()[-1] if name.split() else ""
        id_counts: dict[str, int] = {}  # author_id → frequency across results

        for result in organic:
            authors = result.get("publication_info", {}).get("authors", [])
            for author in authors:
                link = author.get("link", "")
                m = _AUTHOR_ID_RE.search(link)
                if not m:
                    continue
                author_id = m.group(1)
                # Accept if the author's displayed name contains the target's last name
                if last_name and last_name not in author.get("name", "").lower():
                    continue
                id_counts[author_id] = id_counts.get(author_id, 0) + 1

        if not id_counts:
            # Papers exist but no linkable author profile — treat as ambiguous
            titles = [r.get("title", "") for r in organic[:_MAX_PAPERS] if r.get("title")]
            return ScholarResult(status="ambiguous", papers=titles)

        # Pick the author_id with most appearances
        best_id = max(id_counts, key=lambda k: id_counts[k])
        profile_url = f"https://scholar.google.com/citations?user={best_id}"
        papers = self._fetch_papers(best_id)
        status = "found" if len(id_counts) == 1 else "ambiguous"

        return ScholarResult(status=status, papers=papers, profile_url=profile_url)

    def _fetch_papers(self, author_id: str) -> list[str]:
        """Fetch up to _MAX_PAPERS article titles from the Author endpoint."""
        params = {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "api_key": self._api_key,
        }
        results = GoogleSearch(params).get_dict()
        articles = results.get("articles", [])
        return [a["title"] for a in articles[:_MAX_PAPERS] if a.get("title")]


# ---------------------------------------------------------------------------
# Scholarly stub (future backend)
# ---------------------------------------------------------------------------

class ScholarlyBackend:
    """
    Placeholder for a future backend based on the `scholarly` Python library.

    To use: pip install scholarly, implement this class, then set
    scholar_backend: scholarly in config/settings.yaml.
    For now, the SerpAPI backend is the only supported implementation.
    """

    def lookup(self, name: str, institution: str = "") -> ScholarResult:
        raise NotImplementedError(
            "ScholarlyBackend is not yet implemented. "
            "Use SerpAPIScholarBackend instead "
            "(set scholar_backend: serpapi in config/settings.yaml)."
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def lookup_members(
    candidate_pis: list[str],
    config: Config,
    tab_name: str = "Master List",
) -> dict[tuple[str, str], ScholarResult]:
    """
    Run Google Scholar lookups for all members belonging to the candidate PIs.

    Args:
        candidate_pis: List of "{pi_name}|||{institution}" identifiers
                       (output from step1_coarse_filter).
        config:        Loaded Config.
        tab_name:      Sheet tab to read member rows from.

    Returns:
        Dict mapping (pi_name, member_name) → ScholarResult.
        Members already cached within scholar_cache_days are skipped.
    """
    # Parse candidate PI identifiers
    candidate_set: set[tuple[str, str]] = set()
    for ident in candidate_pis:
        parts = ident.split("|||", 1)
        if len(parts) == 2:
            candidate_set.add((parts[0].strip(), parts[1].strip()))

    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)
    cache_cutoff = date.today() - timedelta(days=config.settings.scholar_cache_days)
    backend = SerpAPIScholarBackend(config)

    results: dict[tuple[str, str], ScholarResult] = {}
    total_lookups = 0
    error_count = 0
    flush_interval = config.settings.scholar_flush_interval

    # Pre-count eligible members so we can report progress as N/total.
    eligible_rows = []
    for row in all_rows:
        pi_name = row.get("pi_name", "").strip()
        institution = row.get("institution", "").strip()
        member_name = row.get("member_name", "").strip()
        if not member_name or not pi_name:
            continue
        if (pi_name, institution) not in candidate_set:
            continue
        recent_papers = row.get("recent_papers", "").strip()
        last_enriched_str = row.get("last_enriched", "").strip()
        if recent_papers and last_enriched_str:
            try:
                last_enriched = date.fromisoformat(last_enriched_str)
                if last_enriched >= cache_cutoff:
                    continue
            except ValueError:
                pass
        eligible_rows.append(row)

    total_eligible = len(eligible_rows)
    logger.info("Scholar lookup: %d member(s) to look up", total_eligible)

    lookup_start = time.monotonic()

    for row in eligible_rows:
        pi_name = row.get("pi_name", "").strip()
        institution = row.get("institution", "").strip()
        member_name = row.get("member_name", "").strip()

        result = backend.lookup(member_name, institution)
        results[(pi_name, member_name)] = result
        total_lookups += 1

        if result.status == "error":
            error_count += 1

        # Progress logging: first, every flush_interval, and last lookup
        if total_lookups == 1 or total_lookups % flush_interval == 0 or total_lookups == total_eligible:
            elapsed = time.monotonic() - lookup_start
            avg_per = elapsed / total_lookups
            remaining = total_eligible - total_lookups
            eta = avg_per * remaining
            pct = total_lookups / total_eligible * 100 if total_eligible else 0
            logger.info(
                "Scholar lookup progress: %d/%d (%.0f%%). "
                "Last: %s — %s. Elapsed: %.0fs. Est. remaining: %.0fs.",
                total_lookups, total_eligible, pct,
                member_name, result.status,
                elapsed, eta,
            )

        # Warn and pause if error rate exceeds threshold (check after ≥5 lookups)
        if total_lookups >= 5 and error_count / total_lookups > _ERROR_RATE_THRESHOLD:
            logger.warning(
                "Scholar lookup error rate exceeded 20%% (%d/%d). "
                "Pausing %ds. Consider checking API quota or switching backends.",
                error_count, total_lookups, _ERROR_PAUSE_SECONDS,
            )
            time.sleep(_ERROR_PAUSE_SECONDS)

    found = sum(1 for r in results.values() if r.status == "found")
    ambig = sum(1 for r in results.values() if r.status == "ambiguous")
    not_found = sum(1 for r in results.values() if r.status == "not_found")

    logger.info(
        "lookup_members: %d lookups — %d found, %d ambiguous, %d not_found, %d errors",
        total_lookups, found, ambig, not_found, error_count,
    )
    return results
