"""
Pipeline 1, Step 1: Discover relevant faculty/department URLs for each institution.

For each institution, runs 3 web searches, deduplicates and heuristically scores
the results, then (if >5 candidates remain) calls Haiku to pick the top 3-5 most
likely faculty listing pages.
"""
import json
import logging
import re
from urllib.parse import urlparse

from src import llm, search
from src.config import Config

logger = logging.getLogger(__name__)

# Path segments that strongly indicate a faculty/people listing page
_LISTING_SEGMENTS = frozenset([
    "/people", "/faculty", "/members", "/directory",
    "/team", "/lab", "/labs", "/group", "/researchers",
    "/investigators",
])

# Substrings in the URL path that suggest non-listing content
_NEGATIVE_PATTERNS = [
    "/news/", "/blog/", "/press/", "/event/", "/events/",
    "/publication/", "/publications/", "/article/", "/articles/",
    "/seminar/", "/workshop/", "/course/", "/class/", "/student/",
    "/award/", "/awards/", "/alumni/",
]

_DATE_RE = re.compile(r"/\d{4}/")

# Used to extract a JSON array from Haiku's response even if it adds prose
_JSON_ARRAY_RE = re.compile(r"\[[\d,\s]+\]")


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

def _heuristic_score(url: str) -> int:
    """
    Higher score → more likely to be a faculty listing page.
    Hard-negative URLs (PDFs, date-based paths) get a very low score so they
    fall below the `_is_candidate` threshold.
    """
    path = urlparse(url).path.lower()
    score = 0

    for seg in _LISTING_SEGMENTS:
        if seg in path:
            score += 2
            break  # count at most once

    if url.lower().endswith(".pdf"):
        return -10  # always excluded

    for pat in _NEGATIVE_PATTERNS:
        if pat in path:
            score -= 3
            break

    if _DATE_RE.search(path):
        score -= 2

    return score


def _is_candidate(score: int) -> bool:
    """Exclude URLs whose heuristic score is clearly negative."""
    return score >= -1


# ---------------------------------------------------------------------------
# LLM tiebreaker
# ---------------------------------------------------------------------------

def _llm_filter(institution_name: str, candidates: list[dict]) -> list[dict]:
    """
    Ask Haiku to pick the top 3-5 faculty listing pages from the candidate list.
    Falls back to the top-5 by heuristic order if the response can't be parsed.
    """
    numbered = "\n".join(
        f"{i + 1}. {c['url']} — {c['snippet'][:150]}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"Institution: {institution_name}\n"
        f"Candidate URLs:\n{numbered}\n"
        "Which of these URLs are most likely to be faculty/PI listing pages for "
        "departments related to medical AI, clinical machine learning, or health informatics? "
        "Return ONLY a JSON array of the URL numbers, e.g. [1, 3, 5]. Pick up to 5."
    )
    system = "You select URLs most likely to be faculty listing pages for research departments."

    logger.info("LLM filtering %d candidates for %s", len(candidates), institution_name)
    response = llm.call_haiku(prompt=prompt, system=system)

    match = _JSON_ARRAY_RE.search(response)
    if not match:
        logger.warning(
            "Could not parse LLM response for %s: %r — keeping top 5",
            institution_name, response,
        )
        return candidates[:5]

    try:
        indices = json.loads(match.group())
        selected = [candidates[i - 1] for i in indices if 1 <= i <= len(candidates)]
        if not selected:
            return candidates[:5]
        logger.debug("LLM selected indices %s for %s", indices, institution_name)
        return selected
    except (json.JSONDecodeError, IndexError) as exc:
        logger.warning(
            "LLM response parse error for %s: %s — keeping top 5",
            institution_name, exc,
        )
        return candidates[:5]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def discover_urls(config: Config) -> dict[str, list[dict]]:
    """
    Discover relevant faculty/department URLs for each institution in config.

    Returns:
        dict mapping institution name -> list of {url, snippet, source_query}
    """
    results: dict[str, list[dict]] = {}

    for institution in config.institutions:
        name = institution.name
        logger.info("Discovering URLs for %s", name)

        queries = [
            f'"{name}" medical AI research faculty',
            f'"{name}" artificial intelligence medicine faculty department',
            f'"{name}" clinical machine learning health informatics faculty',
        ]

        # Collect all hits across queries, deduplicating by URL
        seen: set[str] = set()
        raw: list[dict] = []
        for query in queries:
            try:
                hits = search.web_search(query)
            except Exception as exc:
                logger.error("web_search failed for query %r: %s", query, exc)
                continue
            for hit in hits:
                url = hit.get("link", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                raw.append({
                    "url": url,
                    "snippet": hit.get("snippet", ""),
                    "source_query": query,
                })

        logger.debug("%s: %d unique URLs before filtering", name, len(raw))

        # Score and filter
        scored = [(r, _heuristic_score(r["url"])) for r in raw]
        candidates = [
            r
            for r, s in sorted(scored, key=lambda x: x[1], reverse=True)
            if _is_candidate(s)
        ]

        logger.debug("%s: %d candidates after heuristic filter", name, len(candidates))

        # If nothing survived filtering, fall back to top 3 by score
        if not candidates and raw:
            logger.warning(
                "%s: all URLs filtered out — using top 3 by heuristic score", name
            )
            candidates = [r for r, _ in sorted(scored, key=lambda x: x[1], reverse=True)[:3]]

        # LLM tiebreaker only when there are more than 5 candidates
        if len(candidates) > 5:
            candidates = _llm_filter(name, candidates)

        logger.info("%s: final %d URL(s) selected", name, len(candidates))
        results[name] = candidates

    return results
