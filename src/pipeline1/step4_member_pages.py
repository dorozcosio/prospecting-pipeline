"""
Pipeline 1, Step 4: Find the lab-members page for each PI.

Strategy per PI (requires lab_homepage_url):
  1. Fetch lab homepage HTML (cache-first).
  2. Extract all links with BeautifulSoup.
  3. Heuristic: match anchor text or URL path against /people|members?|team|group|lab\\s*members?/i.
  4. 1 match → done.  Multiple matches → score by priority word list.
  5. 0 matches and >20 links → Haiku tiebreaker.
  6. Still nothing → self-check: does the homepage itself list members?
     (count role-term occurrences; if ≥ 3, use homepage as member page)
  7. Record in lab_members_url.
"""
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src import cache, llm
from src.config import Config
from src.pipeline1.step2_pi_extraction import clean_html

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Regex that flags a link as a candidate members/people page
_MEMBER_RE = re.compile(
    r"\bpeople\b|members?\b|\bteam\b|\bgroup\b|lab\s*members?",
    re.IGNORECASE,
)

# Ordered priority for choosing among multiple heuristic matches
_PRIORITY_WORDS = ["people", "members", "team", "group", "personnel", "lab members"]

# Terms that suggest a page already lists lab members inline
_ROLE_TERMS = [
    "phd student", "ph.d. student", "postdoc", "postdoctoral fellow",
    "graduate student", "grad student", "doctoral student",
    "research scientist", "research assistant", "research associate",
    "undergraduate researcher", "undergraduate student",
    "visiting researcher", "visiting scholar",
]

# Number of role-term occurrences required to treat homepage as member page
_ROLE_TERM_THRESHOLD = 3

# Max links shown to Haiku to avoid context bloat
_MAX_LINKS_FOR_LLM = 60


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def _extract_links(html: str, base_url: str) -> list[dict]:
    """Return all unique page links as [{text, url}], relative URLs resolved."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append({"text": a.get_text(strip=True), "url": full_url})

    return links


# ---------------------------------------------------------------------------
# Heuristic matching
# ---------------------------------------------------------------------------

def _heuristic_matches(links: list[dict]) -> list[dict]:
    """Return links whose anchor text or URL path matches the member-page regex."""
    matches = []
    for link in links:
        path = urlparse(link["url"]).path
        if _MEMBER_RE.search(link["text"]) or _MEMBER_RE.search(path):
            matches.append(link)
    return matches


def _pick_best(matches: list[dict]) -> str:
    """
    Choose the best link from multiple heuristic matches.
    Scores by position in _PRIORITY_WORDS; falls back to first match.
    """
    for word in _PRIORITY_WORDS:
        for m in matches:
            if word in m["text"].lower():
                return m["url"]
    return matches[0]["url"]


# ---------------------------------------------------------------------------
# Haiku tiebreaker
# ---------------------------------------------------------------------------

def _haiku_pick_link(pi_name: str, links: list[dict]) -> str | None:
    """Ask Haiku which link most likely points to a lab-members page."""
    candidate_links = links[:_MAX_LINKS_FOR_LLM]
    numbered = "\n".join(
        f"{i + 1}. [{link['text']}] {link['url']}"
        for i, link in enumerate(candidate_links)
    )
    prompt = (
        f"This is the homepage of {pi_name}'s research lab. "
        "Which link most likely points to a page listing lab members or people?\n\n"
        f"Links found on page:\n{numbered}\n\n"
        "Return ONLY the number of the best link, or 0 if none seem relevant."
    )
    system = "You identify navigation links on academic lab websites."

    response = llm.call_haiku(prompt=prompt, system=system)
    m = re.search(r"\b(\d+)\b", response.strip())
    if not m:
        return None
    idx = int(m.group(1))
    if 1 <= idx <= len(candidate_links):
        return candidate_links[idx - 1]["url"]
    return None


# ---------------------------------------------------------------------------
# Self-check: does the homepage itself list members?
# ---------------------------------------------------------------------------

def _homepage_lists_members(html: str) -> bool:
    """
    Heuristic: count role-term occurrences in page text.
    If the total is >= threshold, assume the page itself enumerates members.
    """
    text = clean_html(html).lower()
    total = sum(text.count(term) for term in _ROLE_TERMS)
    return total >= _ROLE_TERM_THRESHOLD


# ---------------------------------------------------------------------------
# Fetch helper
# ---------------------------------------------------------------------------

def _fetch(url: str) -> str | None:
    html = cache.get(url)
    if html is not None:
        return html
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        cache.set(url, resp.text)
        return resp.text
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def find_member_pages(pis: list[dict], config: Config) -> list[dict]:
    """
    Enrich each PI dict with lab_members_url.

    Args:
        pis:    PI dicts from Step 3 (must include lab_homepage_url)
        config: Loaded Config

    Returns:
        New list of PI dicts with lab_members_url added.
    """
    enriched = [dict(pi) for pi in pis]

    for pi in enriched:
        name = pi["name"]
        homepage = pi.get("lab_homepage_url", "")

        if not homepage:
            pi["lab_members_url"] = ""
            continue

        html = _fetch(homepage)
        if not html:
            pi["lab_members_url"] = ""
            continue

        links = _extract_links(html, homepage)
        matches = _heuristic_matches(links)

        if len(matches) == 1:
            member_url = matches[0]["url"]
            logger.debug("%s: heuristic found 1 match → %s", name, member_url)

        elif len(matches) > 1:
            member_url = _pick_best(matches)
            logger.debug(
                "%s: heuristic found %d matches, picked %s", name, len(matches), member_url
            )

        else:
            # Zero heuristic matches
            member_url = None

            if len(links) > 20:
                logger.debug("%s: no heuristic match, trying Haiku (%d links)", name, len(links))
                member_url = _haiku_pick_link(name, links)
                if member_url:
                    logger.debug("%s: Haiku picked %s", name, member_url)

            if not member_url:
                if _homepage_lists_members(html):
                    logger.debug("%s: homepage itself lists members → using homepage", name)
                    member_url = homepage
                else:
                    logger.warning("%s: no lab members page found", name)

        pi["lab_members_url"] = member_url or ""

    found = sum(1 for pi in enriched if pi.get("lab_members_url"))
    logger.info("find_member_pages: %d/%d PIs have a members URL", found, len(enriched))
    return enriched
