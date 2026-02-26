"""
SerpAPI wrapper for web search and Google Scholar lookups.

scholar_backend is read from settings.yaml; only 'serpapi' is implemented.
Swap to the 'scholarly' library by adding a new backend and changing the setting.
"""
import logging
import random
import time

from serpapi import GoogleSearch

from src.config import get_config

logger = logging.getLogger(__name__)


def web_search(query: str, num_results: int = 10) -> list[dict]:
    """
    Run a Google web search via SerpAPI.

    Returns a list of {title, link, snippet} dicts.
    """
    config = get_config()
    params = {
        "engine": "google",
        "q": query,
        "num": num_results,
        "api_key": config.serpapi_api_key,
    }
    results = GoogleSearch(params).get_dict()
    return [
        {
            "title": r.get("title", ""),
            "link": r.get("link", ""),
            "snippet": r.get("snippet", ""),
        }
        for r in results.get("organic_results", [])
    ]


def scholar_search(name: str, institution: str = "") -> dict:
    """
    Look up a researcher on Google Scholar via SerpAPI.

    Returns:
        {
            "status": "found" | "ambiguous" | "not_found" | "error",
            "papers": list[str],   # paper titles (up to 10)
            "profile_url": str | None,
        }
    """
    config = get_config()
    backend = config.settings.scholar_backend

    if backend != "serpapi":
        raise NotImplementedError(f"scholar_backend '{backend}' is not implemented")

    return _scholar_via_serpapi(name, institution, config)


def _scholar_via_serpapi(name: str, institution: str, config) -> dict:
    query = f"{name} {institution}".strip()

    delay = random.uniform(*config.settings.scholar_delay_range)
    logger.debug("Scholar search for %r — sleeping %.1fs", name, delay)
    time.sleep(delay)

    try:
        params = {
            "engine": "google_scholar",
            "q": query,
            "num": 10,
            "api_key": config.serpapi_api_key,
        }
        results = GoogleSearch(params).get_dict()
        organic = results.get("organic_results", [])

        if not organic:
            return {"status": "not_found", "papers": [], "profile_url": None}

        papers = [r.get("title", "") for r in organic]

        # Try to extract a profile URL from author links in the results
        profile_url: str | None = None
        name_lower = name.lower()
        for result in organic:
            for author in result.get("authors", []):
                if name_lower in author.get("name", "").lower():
                    profile_url = author.get("link")
                    break
            if profile_url:
                break

        # Ambiguity check: if we found no author link matching the name
        # and there are many results, the query may match multiple people
        if profile_url is None and len(organic) > 3:
            return {"status": "ambiguous", "papers": papers, "profile_url": None}

        return {"status": "found", "papers": papers, "profile_url": profile_url}

    except Exception as exc:
        logger.error("Scholar search error for %r: %s", name, exc)
        return {"status": "error", "papers": [], "profile_url": None}
