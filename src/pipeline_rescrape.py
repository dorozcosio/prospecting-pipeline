"""
Targeted Lab Member Re-Discovery + P2 Enrichment.

Bypasses Pipeline 1's institution-page chain and searches directly for PI labs
by name, then runs P2 enrichment on the results.

Motivation: P1's discovery chain (institution faculty listing → lab homepage →
"People" link) is brittle — bad URLs, off-domain lab sites, and missing "People"
links cause trainees to be missed. This module provides a direct path for
specific PIs.

Public API:
  score_lab_url()         — Score a URL for likelihood of being a lab homepage.
  diff_members()          — Compare scraped vs. existing sheet members.
  discover_lab_and_members() — Phase A: find lab page, scrape members.
  enrich_members()        — Phase B: run P2 enrichment on discovery results.
  rescrape_and_enrich()   — Top-level orchestrator tying A and B together.
"""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import json
import requests

from src import cache, search
from src.config import Config
from src.name_utils import normalize_name
from src.pipeline1.step2_pi_extraction import clean_html
from src.pipeline1.step3_lab_homepages import summarize_lab_pages
from src.pipeline1.step4_member_pages import find_member_pages
from src.pipeline1.step5_member_extraction import extract_members
from src.pipeline1.step6_sheet_writer import init_run_cache, write_pipeline1_results
from src.pipeline2.step2_scholar_lookup import lookup_members
from src.pipeline2.step3_fine_scoring import score_relevance
from src.pipeline2.step4_email_lookup import lookup_emails
from src.pipeline2.step5_sheet_writer import write_pipeline2_results
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Phrases in a page summary that signal this is NOT a lab homepage
_NON_LAB_PHRASES = re.compile(
    r"course page|publication listing|news article|search results",
    re.IGNORECASE,
)

# Maximum characters of cleaned text passed to the summarizer per page
_MAX_PAGE_CHARS = 8_000


# ---------------------------------------------------------------------------
# Helper: score_lab_url
# ---------------------------------------------------------------------------

def score_lab_url(
    url: str,
    pi_last_name: str,
    institution_domain: str | None,
    config: Config,
) -> float:
    """
    Score a URL for likelihood of being the PI's lab homepage.

    Scoring rules:
      Base score: 1.0
      +0.3 each: URL contains PI's last name (case-insensitive);
                 URL domain matches institution_domain or ends with .edu;
                 URL path contains any boost keyword from config.
      -0.8 each: URL domain is in the penalize list;
                 URL ends with .pdf.
      Minimum returned value: 0.0.
    """
    score = 1.0
    parsed = urlparse(url)
    domain = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    url_lower = url.lower()

    # Boosts
    if pi_last_name and pi_last_name.lower() in url_lower:
        score += 0.3
    if institution_domain and (
        domain == institution_domain.lower()
        or domain.endswith("." + institution_domain.lower())
    ) or domain.endswith(".edu"):
        score += 0.3
    if any(kw in path for kw in config.settings.rescrape.lab_url_boost_keywords):
        score += 0.3

    # Penalties
    if any(domain == pen or domain.endswith("." + pen)
           for pen in config.settings.rescrape.lab_url_penalize_domains):
        score -= 0.8
    if url_lower.endswith(".pdf"):
        score -= 0.8

    return max(0.0, score)


# ---------------------------------------------------------------------------
# Helper: diff_members
# ---------------------------------------------------------------------------

def diff_members(
    existing_rows: list[dict],
    scraped_members: list[dict],
    pi_name: str,
    institution: str,
) -> tuple[list[dict], list[dict]]:
    """
    Compare scraped members against what's already in the sheet for a given PI.

    Uses normalize_name() for all comparisons so that name variants (middle
    initials, extra spaces, unicode differences) are treated as identical.

    Args:
        existing_rows:   Sheet rows for this PI (already filtered by pi_name/institution).
        scraped_members: Output of extract_members() for this PI.
        pi_name:         PI name (for populating missing P1 fields on new rows).
        institution:     Institution name.

    Returns:
        (new_members, missing_members)
        new_members:     Rows in scraped but not in existing — ready to write.
        missing_members: Rows in existing but not in scraped — logged only,
                         NOT deactivated.
    """
    existing_norm = {
        normalize_name(r.get("member_name", "")): r
        for r in existing_rows
        if r.get("member_name")
    }
    scraped_norm = {
        normalize_name(m.get("member_name", "")): m
        for m in scraped_members
        if m.get("member_name")
    }

    new_members = [
        m for norm, m in scraped_norm.items()
        if norm not in existing_norm
    ]
    missing_members = [
        r for norm, r in existing_norm.items()
        if norm not in scraped_norm
    ]
    return new_members, missing_members


# ---------------------------------------------------------------------------
# Helper: fetch page (cache-first)
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str | None:
    html = cache.get(url)
    if html is not None:
        return html
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        cache.set(url, resp.text)
        return resp.text
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Phase A: discover_lab_and_members
# ---------------------------------------------------------------------------

def discover_lab_and_members(
    pi: dict,
    config: Config,
    tab_name: str = "Master List",
) -> dict:
    """
    Phase A: Directly search for a PI's lab homepage, then scrape lab members.

    Args:
        pi:       Dict with keys: pi_name, institution.
        config:   Loaded Config.
        tab_name: Sheet tab to read existing rows from.

    Returns:
        {
            "pi_name": str,
            "institution": str,
            "lab_homepage_url": str,
            "lab_research_summary": str,
            "lab_members_url": str,
            "all_members": list[dict],
            "new_members": list[dict],
            "missing_members": list[dict],
        }
        Returns empty all_members/new_members if no valid lab page found.
    """
    pi_name = pi["pi_name"]
    institution = pi["institution"]
    last_name = pi_name.strip().split()[-1] if pi_name.strip() else ""

    # Resolve institution domain for scoring
    institution_domain: str | None = None
    for inst in config.institutions:
        if inst.name == institution:
            institution_domain = inst.domain
            break

    empty_result = {
        "pi_name": pi_name,
        "institution": institution,
        "lab_homepage_url": "",
        "lab_research_summary": "",
        "lab_members_url": "",
        "all_members": [],
        "new_members": [],
        "missing_members": [],
    }

    # ------------------------------------------------------------------
    # Step A1: Web search — two queries, deduplicate and score
    # ------------------------------------------------------------------
    queries = [
        f'"{pi_name}" lab {institution}',
        f'"{pi_name}" research group {institution}',
    ]
    seen_urls: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        try:
            hits = search.web_search(query, num_results=5)
        except Exception as exc:
            logger.warning("Web search failed for %r: %s", query, exc)
            hits = []
        for hit in hits:
            url = hit.get("link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                score = score_lab_url(url, last_name, institution_domain, config)
                candidates.append({"url": url, "score": score, "hit": hit})

    if not candidates:
        logger.warning("No search results for %s at %s", pi_name, institution)
        return empty_result

    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Keep top candidates — keep both top-2 if scores are within 10% of each other
    max_cands = config.settings.rescrape.max_lab_search_candidates
    top_score = candidates[0]["score"]
    selected = [candidates[0]]
    if len(candidates) > 1:
        second = candidates[1]
        if top_score > 0 and abs(top_score - second["score"]) / top_score <= 0.10:
            selected.append(second)
    selected = selected[:max_cands]

    logger.debug(
        "discover_lab_and_members: %d candidate(s) for %s (top score %.2f)",
        len(selected), pi_name, top_score,
    )

    # ------------------------------------------------------------------
    # Step A2: Validate candidates — fetch, clean, summarize, filter
    # ------------------------------------------------------------------
    lab_homepage_url = ""
    lab_research_summary = ""

    for cand in selected:
        url = cand["url"]
        html = _fetch_page(url)
        if not html:
            continue

        cleaned = clean_html(html)[:_MAX_PAGE_CHARS]
        summaries = summarize_lab_pages([(pi_name, url, cleaned)], config)
        summary = summaries.get(pi_name, "")

        if not summary or _NON_LAB_PHRASES.search(summary):
            logger.debug(
                "Discarding candidate %s for %s — non-lab summary: %r",
                url, pi_name, summary[:120],
            )
            continue

        lab_homepage_url = url
        lab_research_summary = summary
        logger.info("Lab homepage confirmed for %s: %s", pi_name, url)
        break

    if not lab_homepage_url:
        logger.warning(
            "Could not find valid lab homepage for %s at %s after %d attempt(s)",
            pi_name, institution, len(selected),
        )
        return empty_result

    # ------------------------------------------------------------------
    # Step A3: Find member page and scrape members
    # ------------------------------------------------------------------
    # Build the PI dict format expected by step4 / step5
    pi_dict = {
        "name": pi_name,
        "institution": institution,
        "lab_homepage_url": lab_homepage_url,
        "lab_research_summary": lab_research_summary,
        "lab_members_url": "",
        "department_program": "",
        "role": "PI",
        "source_url": "",
    }

    enriched_pis = find_member_pages([pi_dict], config)
    pi_dict = enriched_pis[0]
    lab_members_url = pi_dict.get("lab_members_url", "")

    scraped_rows = extract_members([pi_dict], config)

    # Read existing sheet rows for this PI
    client = SheetsClient(config)
    all_sheet_rows = client.read_all_rows(tab_name)
    existing_rows = [
        r for r in all_sheet_rows
        if normalize_name(r.get("pi_name", "")) == normalize_name(pi_name)
        and normalize_name(r.get("institution", "")) == normalize_name(institution)
    ]

    new_members, missing_members = diff_members(existing_rows, scraped_rows, pi_name, institution)

    if missing_members:
        missing_names = [m.get("member_name", "?") for m in missing_members]
        logger.warning(
            "Members in sheet but not on current lab page for %s: %s. "
            "Not deactivating — flagged for manual review.",
            pi_name, ", ".join(missing_names),
        )

    logger.info(
        "discover_lab_and_members: %s — %d total member(s), %d new, %d missing",
        pi_name, len(scraped_rows), len(new_members), len(missing_members),
    )

    # all_members = existing rows updated with fresh PI-level fields + new rows
    # (scraped_rows already includes PI row + all members)
    return {
        "pi_name": pi_name,
        "institution": institution,
        "lab_homepage_url": lab_homepage_url,
        "lab_research_summary": lab_research_summary,
        "lab_members_url": lab_members_url,
        "all_members": scraped_rows,
        "new_members": new_members,
        "missing_members": missing_members,
    }


# ---------------------------------------------------------------------------
# Phase B: enrich_members
# ---------------------------------------------------------------------------

def enrich_members(
    discovery_results: list[dict],
    situation: str,
    config: Config,
    skip_scholar: bool = False,
    force: bool = False,
    tab_name: str = "Master List",
) -> dict:
    """
    Phase B: Run P2 enrichment on the PI labs from Phase A.

    Skips the coarse filter — these PIs are pre-selected.

    Args:
        discovery_results: List of dicts from discover_lab_and_members().
        situation:         Free-text situation of interest.
        config:            Loaded Config.
        skip_scholar:      If True, skip Google Scholar lookups.
        force:             Reserved for future cache-busting support.
        tab_name:          Sheet tab to read member rows from.

    Returns:
        {"scholar_results": dict, "relevance_scores": list[dict], "emails": dict}
    """
    # Build candidate PI identifiers
    candidate_pis = [
        f"{r['pi_name']}|||{r['institution']}"
        for r in discovery_results
        if r.get("pi_name") and r.get("institution")
    ]

    if not candidate_pis:
        logger.warning("enrich_members: no valid PI identifiers — returning empty")
        return {"scholar_results": {}, "relevance_scores": [], "emails": {}}

    # Step 2: Scholar lookup (optional)
    scholar_results: dict = {}
    if not skip_scholar:
        logger.info("enrich_members: Scholar lookup for %d PI(s)", len(candidate_pis))
        scholar_results = lookup_members(candidate_pis, config, tab_name=tab_name)
    else:
        logger.info("enrich_members: Scholar lookup skipped (--skip-scholar)")

    # Step 3: Relevance scoring
    logger.info("enrich_members: relevance scoring for %d PI(s)", len(candidate_pis))
    relevance_scores = score_relevance(
        candidate_pis, scholar_results, situation, config, tab_name=tab_name
    )

    # Step 4: Email lookup for flagged members
    flagged = [s for s in relevance_scores if s.get("relevance_flag") and not s.get("email")]
    emails: dict = {}
    if flagged:
        logger.info("enrich_members: email lookup for %d flagged member(s)", len(flagged))
        emails = lookup_emails(flagged, config)
    else:
        logger.info("enrich_members: no flagged members without emails — skipping email lookup")

    return {
        "scholar_results": scholar_results,
        "relevance_scores": relevance_scores,
        "emails": emails,
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def rescrape_and_enrich(
    pis: list[dict],
    situation: str,
    config: Config,
    dry_run: bool = False,
    skip_scholar: bool = False,
    force: bool = False,
    tab_name: str = "Master List",
) -> dict:
    """
    Targeted lab member re-discovery + P2 enrichment.

    Phase A — for each PI, find their lab homepage directly (bypassing P1's
    institution-page chain), scrape members, and diff against the sheet.

    Phase B — run Scholar lookup, relevance scoring, and email lookup for the
    target PIs. Writes results to the sheet unless dry_run=True.

    Args:
        pis:          List of {pi_name, institution} dicts.
        situation:    Free-text situation of interest.
        config:       Loaded Config.
        dry_run:      If True, skip all sheet writes and save output to JSON.
        skip_scholar: If True, skip Scholar lookup in Phase B.
        force:        Reserved for future cache-busting support.
        tab_name:     Sheet tab to read/write.

    Returns:
        {
            "pis_processed": int,
            "lab_homepages_updated": int,
            "new_members_found": int,
            "scholar_lookups_run": int,
            "members_flagged_relevant": int,
            "emails_found": int,
        }
    """
    logger.info(
        "rescrape_and_enrich: %d PI(s), situation=%r, dry_run=%s, skip_scholar=%s",
        len(pis), situation, dry_run, skip_scholar,
    )

    # ------------------------------------------------------------------
    # Phase A: Discovery
    # ------------------------------------------------------------------
    discovery_results: list[dict] = []
    all_rows_to_write: list[dict] = []
    institutions_in_run: list[str] = []

    for pi in pis:
        result = discover_lab_and_members(pi, config, tab_name=tab_name)
        discovery_results.append(result)
        if result["lab_homepage_url"]:
            # Collect all scraped rows (existing with updated fields + new)
            all_rows_to_write.extend(result["all_members"])
            inst = pi["institution"]
            if inst not in institutions_in_run:
                institutions_in_run.append(inst)

    lab_homepages_updated = sum(1 for r in discovery_results if r["lab_homepage_url"])
    new_members_found = sum(len(r["new_members"]) for r in discovery_results)

    # Write Phase A results to sheet
    if not dry_run and all_rows_to_write:
        init_run_cache()
        write_result = write_pipeline1_results(
            all_rows_to_write,
            config,
            institutions_in_run=institutions_in_run,
            tab_name=tab_name,
            deactivate_missing=False,  # never deactivate in rescrape
        )
        logger.info("Phase A sheet write: %s", write_result)

    # ------------------------------------------------------------------
    # Phase B: Enrichment
    # ------------------------------------------------------------------
    enrichment = enrich_members(
        discovery_results,
        situation,
        config,
        skip_scholar=skip_scholar,
        force=force,
        tab_name=tab_name,
    )

    scholar_results = enrichment["scholar_results"]
    relevance_scores = enrichment["relevance_scores"]
    emails = enrichment["emails"]

    members_flagged_relevant = sum(1 for s in relevance_scores if s.get("relevance_flag"))
    emails_found = sum(1 for v in emails.values() if v)
    scholar_lookups_run = len(scholar_results)

    # Write Phase B results to sheet
    if not dry_run and relevance_scores:
        write_pipeline2_results(
            relevance_scores,
            scholar_results,
            emails,
            situation,
            config,
            tab_name=tab_name,
        )

    # Dry-run: save full output to JSON
    if dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path("logs") / f"rescrape_dry_run_{ts}.json"
        out_path.parent.mkdir(exist_ok=True)
        payload = {
            "pis": pis,
            "situation": situation,
            "discovery_results": [
                {k: v for k, v in r.items() if k != "all_members"}
                | {"all_members_count": len(r.get("all_members", []))}
                for r in discovery_results
            ],
            "relevance_scores": relevance_scores,
            "emails": {str(k): v for k, v in emails.items()},
        }
        with out_path.open("w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info("[DRY RUN] Output saved to %s", out_path)

    summary = {
        "pis_processed": len(pis),
        "lab_homepages_updated": lab_homepages_updated,
        "new_members_found": new_members_found,
        "scholar_lookups_run": scholar_lookups_run,
        "members_flagged_relevant": members_flagged_relevant,
        "emails_found": emails_found,
    }
    logger.info("rescrape_and_enrich summary: %s", summary)
    return summary
