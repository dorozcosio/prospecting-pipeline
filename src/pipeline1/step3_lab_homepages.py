"""
Pipeline 1, Step 3: Find each PI's lab homepage and summarize their research focus.

For each PI:
  - Run 2 targeted web searches
  - Check 2 common URL patterns (HEAD request)
  - Fetch the best candidate (cache-first)
  - Batch homepage texts for summarization:
      * Haiku for pages ≤ 10 000 chars (fast, cheap)
      * Sonnet for longer / complex pages
    Batch size comes from config (haiku_batch_size / sonnet_batch_size).
"""
import json
import logging
import re

import requests

from src import cache, llm, search
from src.config import Config
from src.pipeline1.step2_pi_extraction import chunks, clean_html, extract_json_str

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Pages longer than this are routed to Sonnet instead of Haiku
_SONNET_THRESHOLD = 10_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _institution_domain(institution_name: str, config: Config) -> str:
    for inst in config.institutions:
        if inst.name == institution_name:
            return inst.domain
    return ""


def _check_url_alive(url: str) -> bool:
    """Return True if the URL responds with HTTP 200."""
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True, headers=_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False


def _fetch_page(url: str) -> str | None:
    """Fetch a URL, using cache. Returns raw HTML or None on failure."""
    html = cache.get(url)
    if html is not None:
        logger.debug("Cache hit for %s", url)
        return html
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        cache.set(url, resp.text)
        logger.debug("Fetched and cached %s", url)
        return resp.text
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return None


def _best_candidate(results: list[dict], domain: str) -> str | None:
    """
    Pick the most likely lab homepage URL.
    Prefers URLs on the institution domain; falls back to first result.
    """
    for r in results:
        url = r.get("link") or r.get("url", "")
        if url and domain and domain in url:
            return url
    for r in results:
        url = r.get("link") or r.get("url", "")
        if url:
            return url
    return None


def _choose_model_for_summary(text: str) -> str:
    """Use Haiku for short pages, Sonnet for long ones (> _SONNET_THRESHOLD chars)."""
    return "sonnet" if len(text) > _SONNET_THRESHOLD else "haiku"


# ---------------------------------------------------------------------------
# LLM summarisation
# ---------------------------------------------------------------------------

def _summarize_batch(batch: list[dict], model: str = "haiku") -> dict[str, str]:
    """
    Summarize a batch of lab homepages with the specified model.
    Returns {pi_name: one_or_two_sentence_summary}.
    """
    parts = [
        f"=== LAB {i + 1}: {item['name']} ({item['url']}) ===\n{item['text']}"
        for i, item in enumerate(batch)
    ]
    user_prompt = (
        "Summarize each lab's research focus in 1–2 sentences.\n\n"
        + "\n\n".join(parts)
        + "\n\n"
        "Return a JSON object keyed by the PI name (use the exact name from the "
        "=== LAB === header) with the summary string as value.\n"
        "Example:\n"
        '{"Jane Smith": "Studies computational approaches to protein folding using '
        'deep learning, with applications in drug discovery."}'
    )
    system = (
        "You summarize research lab focus areas. "
        "Be specific about methods, model systems, and application areas."
    )

    logger.debug("Summarizing %d labs with %s", len(batch), model)
    if model == "sonnet":
        response = llm.call_sonnet(prompt=user_prompt, system=system)
    else:
        response = llm.call_haiku(prompt=user_prompt, system=system)

    json_str = extract_json_str(response)

    try:
        result = json.loads(json_str)
        if not isinstance(result, dict):
            raise ValueError(f"Expected dict, got {type(result).__name__}")
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("JSON parse error in _summarize_batch: %s | snippet: %.300s", exc, response)
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def find_lab_homepages(pis: list[dict], config: Config) -> list[dict]:
    """
    Enrich each PI dict with lab_homepage_url and lab_research_summary.

    Args:
        pis:    List of PI dicts from Step 2 ({name, role, institution, …})
        config: Loaded Config object

    Returns:
        New list of PI dicts with lab_homepage_url and lab_research_summary added.
    """
    enriched = [dict(pi) for pi in pis]  # work on copies
    pages_to_summarize: list[dict] = []

    for pi in enriched:
        name = pi["name"]
        institution = pi.get("institution", "")
        domain = _institution_domain(institution, config)
        last_name = name.split()[-1].lower() if name.split() else ""

        # 1. Web searches
        all_candidates: list[dict] = []
        queries = [
            f'"{name}" lab site:{domain}' if domain else f'"{name}" research lab',
            f'"{name}" research group {institution}',
        ]
        for query in queries:
            try:
                hits = search.web_search(query, num_results=5)
                all_candidates.extend(hits)
            except Exception as exc:
                logger.error("web_search failed for %r: %s", query, exc)

        # 2. Common URL patterns — checked via HEAD request
        if domain and last_name:
            for pattern in [
                f"https://{domain}/~{last_name}",
                f"https://{domain}/labs/{last_name}",
            ]:
                if _check_url_alive(pattern):
                    logger.debug("Pattern URL live for %s: %s", name, pattern)
                    all_candidates.insert(0, {"link": pattern, "snippet": ""})

        # 3. Pick best URL
        best_url = _best_candidate(all_candidates, domain)
        if not best_url:
            logger.warning("No lab homepage found for %s (%s)", name, institution)
            pi["lab_homepage_url"] = ""
            pi["lab_research_summary"] = ""
            continue

        # 4. Fetch and cache
        html = _fetch_page(best_url)
        if not html:
            pi["lab_homepage_url"] = ""
            pi["lab_research_summary"] = ""
            continue

        pi["lab_homepage_url"] = best_url
        pages_to_summarize.append({
            "name": name,
            "url": best_url,
            "text": clean_html(html),
        })

    # Ensure every PI has these keys even if not summarized
    for pi in enriched:
        pi.setdefault("lab_homepage_url", "")
        pi.setdefault("lab_research_summary", "")

    # 5. Batch summarisation — split by model based on page length
    name_to_pi = {pi["name"]: pi for pi in enriched}

    haiku_pages = [p for p in pages_to_summarize if _choose_model_for_summary(p["text"]) == "haiku"]
    sonnet_pages = [p for p in pages_to_summarize if _choose_model_for_summary(p["text"]) == "sonnet"]

    if sonnet_pages:
        logger.info(
            "find_lab_homepages: %d pages → Haiku, %d pages → Sonnet",
            len(haiku_pages), len(sonnet_pages),
        )

    for model, pages, batch_size in (
        ("haiku", haiku_pages, config.settings.haiku_batch_size),
        ("sonnet", sonnet_pages, config.settings.sonnet_batch_size),
    ):
        if not pages:
            continue
        texts = [p["text"] for p in pages]
        for index_batch in llm.build_batches(texts, max_items=batch_size):
            batch = [pages[i] for i in index_batch]
            summaries = _summarize_batch(batch, model=model)
            for item in batch:
                pi_ref = name_to_pi.get(item["name"])
                if pi_ref is not None:
                    pi_ref["lab_research_summary"] = summaries.get(item["name"], "")

    found = sum(1 for pi in enriched if pi.get("lab_homepage_url"))
    logger.info("find_lab_homepages: %d/%d PIs got a homepage", found, len(enriched))
    return enriched
