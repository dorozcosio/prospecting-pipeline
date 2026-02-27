"""
Pipeline 1, Step 2: Extract PI/faculty names from listing pages.

For each URL from Step 1:
  - Fetch HTML (cache-first)
  - Strip noise tags, truncate to ~8 000 tokens
  - Batch pages into a single Sonnet call (batch size from config)
  - Parse the JSON response, deduplicate names
"""
import json
import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src import cache, llm
from src.config import Config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_MAX_PAGE_CHARS = 32_000  # ≈ 8 000 tokens

# URL path/host fragments → human-readable department labels
_DEPT_HINTS: dict[str, str] = {
    "csail": "CSAIL",
    "eecs": "EECS",
    "imes": "IMES",
    "hst": "Health Sciences & Technology",
    "bcs": "Brain & Cognitive Sciences",
    "biology": "Biology",
    "csbphd": "Computational Science & Biology",
    "mathematics": "Mathematics",
    "math": "Mathematics",
    "physics": "Physics",
    "chemistry": "Chemistry",
    "medicine": "Medicine",
    "dbmi": "Biomedical Informatics",
    "bmi": "Biomedical Informatics",
    "hds": "Health Data Science",
    "informatics": "Health Informatics",
    "ml-and-healthcare": "ML & Healthcare",
    "ai-for-healthcare": "AI for Healthcare & Life Sciences",
    "dbds": "Data & Biomedical Data Science",
}

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")
_MIDDLE_INITIAL_RE = re.compile(r"\b[A-Z]\.\s+")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Shared utilities (also imported by step3)
# ---------------------------------------------------------------------------

def clean_html(html: str, max_chars: int = _MAX_PAGE_CHARS) -> str:
    """Strip nav/footer/script/style, return plain text truncated to max_chars."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["nav", "footer", "script", "style",
                               "head", "aside", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def extract_json_str(text: str) -> str:
    """Extract the JSON content from a possible ```json … ``` code block."""
    m = _JSON_BLOCK_RE.search(text)
    return m.group(1) if m else text.strip()


def chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ---------------------------------------------------------------------------
# Department inference
# ---------------------------------------------------------------------------

def _infer_department(url: str) -> str:
    parsed = urlparse(url)
    search_space = (parsed.hostname or "") + parsed.path
    search_space = search_space.lower()
    for hint, label in _DEPT_HINTS.items():
        if hint in search_space:
            return label
    return ""


# ---------------------------------------------------------------------------
# Name deduplication
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Lowercase, remove middle initials, collapse whitespace."""
    name = name.lower().strip()
    name = _MIDDLE_INITIAL_RE.sub("", name)
    return _WHITESPACE_RE.sub(" ", name).strip()


def _deduplicate(pis: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for pi in pis:
        key = _normalize_name(pi["name"])
        if key not in seen:
            seen[key] = pi
        elif pi.get("role") and not seen[key].get("role"):
            seen[key] = pi  # prefer the entry that has a role
    return list(seen.values())


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def _extract_batch(pages: list[dict]) -> dict[str, list[dict]]:
    """
    Send up to BATCH_SIZE pages to Sonnet.
    Returns {page_url: [{name, role}, …]}.
    """
    parts = [
        f"=== PAGE {i + 1} ({p['url']}) ===\n{p['text']}"
        for i, p in enumerate(pages)
    ]
    user_prompt = (
        "Extract all faculty / Principal Investigator names from the following pages. "
        "For each person, return their name and title/role if listed.\n\n"
        + "\n\n".join(parts)
        + "\n\n"
        "Return a JSON object with one key per page URL (use the exact URL shown in the "
        "=== PAGE === header), each containing an array of {name, role} objects. "
        "If no faculty are found on a page, return an empty array for that page.\n"
        "Example:\n"
        '{"https://example.edu/faculty": [{"name": "Jane Smith", "role": "Associate Professor"}]}'
    )
    system = (
        "You extract faculty and PI names from university web pages. "
        "Return structured JSON only."
    )

    response = llm.call_sonnet(prompt=user_prompt, system=system)
    json_str = extract_json_str(response)

    try:
        result = json.loads(json_str)
        if not isinstance(result, dict):
            raise ValueError(f"Expected dict, got {type(result).__name__}")
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("JSON parse error in _extract_batch: %s | snippet: %.300s", exc, response)
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_pis(institution: str, urls: list[dict], config: Config) -> list[dict]:
    """
    Fetch each URL (cache-first), extract faculty/PI names with Sonnet, deduplicate.

    Args:
        institution: Institution name string (e.g. "MIT")
        urls:        List of {url, snippet, source_query} dicts from Step 1
        config:      Loaded Config object

    Returns:
        List of {name, role, institution, department_program, source_url} dicts
    """
    # Phase 1 – fetch and clean pages
    pages: list[dict] = []
    for entry in urls:
        url = entry["url"]
        html = cache.get(url)
        if html is None:
            try:
                resp = requests.get(url, timeout=15, headers=_HEADERS)
                resp.raise_for_status()
                html = resp.text
                cache.set(url, html)
                logger.debug("Fetched and cached %s", url)
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", url, exc)
                continue
        else:
            logger.debug("Cache hit for %s", url)

        pages.append({
            "url": url,
            "text": clean_html(html),
            "dept": _infer_department(url),
        })

    if not pages:
        logger.warning("No pages fetched for %s — returning empty list", institution)
        return []

    dept_map = {p["url"]: p["dept"] for p in pages}

    # Phase 2 – batch Sonnet calls (token-aware batching)
    all_pis: list[dict] = []
    batch_size = config.settings.sonnet_batch_size
    page_texts = [p["text"] for p in pages]
    for index_batch in llm.build_batches(page_texts, max_items=batch_size):
        batch = [pages[i] for i in index_batch]
        extracted = _extract_batch(batch)

        for page in batch:
            # Try exact URL match first, then fall back to any key that contains
            # the page's base path (handles URLs where query params are dropped)
            entries = extracted.get(page["url"])
            if entries is None:
                for key, val in extracted.items():
                    if urlparse(page["url"]).path in key or key in page["url"]:
                        entries = val
                        break

            if not entries or not isinstance(entries, list):
                continue

            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                all_pis.append({
                    "name": entry["name"].strip(),
                    "role": entry.get("role", ""),
                    "institution": institution,
                    "department_program": dept_map.get(page["url"], ""),
                    "source_url": page["url"],
                })

    logger.info(
        "%s: extracted %d raw PIs → %d after deduplication",
        institution, len(all_pis), len(_deduplicate(all_pis)),
    )
    return _deduplicate(all_pis)
