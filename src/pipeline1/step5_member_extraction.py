"""
Pipeline 1, Step 5: Extract lab member names/roles and build flat row dicts.

For each PI with a lab_members_url:
  - Fetch the members page (cache-first), clean HTML
  - Route to Haiku (default) or Sonnet (long + unstructured pages)
  - Batch pages per model using token-aware batching from config
  - Parse {pi_name: [{name, role}]} JSON response
  - Emit one row per member + one "PI" row for the PI themselves
  - Each row carries all known PI-level fields so it is ready to write to Sheets
"""
import json
import logging

import requests

from src import cache, llm
from src.config import Config
from src.pipeline1.step2_pi_extraction import (
    chunks,
    clean_html,
    extract_json_str,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Role keywords that indicate a structured member list (used for model routing)
_ROLE_KEYWORDS = [
    "postdoc", "phd student", "graduate student", "research scientist",
    "research assistant", "undergraduate", "professor", "fellow",
    "ph.d.", "research associate", "lab manager",
]


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

def _choose_model_for_member_extraction(cleaned_text: str) -> str:
    """
    Use Haiku for most pages.
    Only escalate to Sonnet for long, unstructured pages (>8000 chars AND <2 role keywords).
    """
    role_count = sum(1 for kw in _ROLE_KEYWORDS if kw.lower() in cleaned_text.lower())
    if len(cleaned_text) > 8000 and role_count < 2:
        return "sonnet"
    return "haiku"


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
# LLM extraction
# ---------------------------------------------------------------------------

def _extract_batch(batch: list[dict], model: str = "haiku") -> dict[str, list[dict]]:
    """
    Send a batch of member pages to the specified model.
    Returns {pi_name: [{name, role}, …]}.
    """
    parts = [
        f"=== LAB {i + 1}: {item['pi_name']} lab ({item['url']}) ===\n{item['text']}"
        for i, item in enumerate(batch)
    ]
    user_prompt = (
        "Extract all lab members from the following pages. "
        "For each person, return their name and role/title "
        "(e.g., Postdoc, PhD Student, Research Scientist, Research Assistant, Undergraduate).\n\n"
        + "\n\n".join(parts)
        + "\n\n"
        "Return a JSON object keyed by PI name (use the exact name from the === LAB === header), "
        "each containing an array of {name, role} objects. "
        "If no members are found, return an empty array.\n"
        "Example:\n"
        '{"Jane Smith": [{"name": "Alex Johnson", "role": "PhD Student"}, '
        '{"name": "Maria Garcia", "role": "Postdoc"}]}'
    )
    system = (
        "You extract lab member information from academic web pages. "
        "Return structured JSON only."
    )

    logger.debug("Extracting members from %d pages with %s", len(batch), model)
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
        logger.warning(
            "JSON parse error in _extract_batch: %s | snippet: %.300s", exc, response
        )
        return {}


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _pi_fields(pi: dict) -> dict:
    """Extract the PI-level fields shared across all rows for this PI."""
    return {
        "institution": pi.get("institution", ""),
        "department_program": pi.get("department_program", ""),
        "pi_name": pi.get("name", ""),
        "lab_research_summary": pi.get("lab_research_summary", ""),
        "lab_homepage_url": pi.get("lab_homepage_url", ""),
        "lab_members_url": pi.get("lab_members_url", ""),
    }


def _pi_row(pi: dict) -> dict:
    """Row representing the PI themselves (member_role = 'PI')."""
    row = _pi_fields(pi)
    row["member_name"] = pi.get("name", "")
    row["member_role"] = "PI"
    return row


def _member_row(pi: dict, member: dict) -> dict:
    """Row representing a single lab member."""
    row = _pi_fields(pi)
    row["member_name"] = member.get("name", "").strip()
    row["member_role"] = member.get("role", "")
    return row


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_members(pis: list[dict], config: Config) -> list[dict]:
    """
    For each PI with a lab_members_url, fetch the page and extract members.

    Returns a flat list of row dicts, one per person (PI row + member rows).
    Each row has: institution, department_program, pi_name, lab_research_summary,
    lab_homepage_url, lab_members_url, member_name, member_role.
    """
    # Phase 1 — fetch member pages and route to model
    haiku_items: list[dict] = []
    sonnet_items: list[dict] = []
    pi_no_page: list[dict] = []

    for pi in pis:
        members_url = pi.get("lab_members_url", "")
        if not members_url:
            pi_no_page.append(pi)
            continue

        html = _fetch(members_url)
        if not html:
            pi_no_page.append(pi)
            continue

        text = clean_html(html)
        model = _choose_model_for_member_extraction(text)
        item = {
            "pi": pi,
            "pi_name": pi["name"],
            "url": members_url,
            "text": text,
        }
        logger.debug("Routing %s member page to %s", pi["name"], model)
        if model == "sonnet":
            sonnet_items.append(item)
        else:
            haiku_items.append(item)

    if sonnet_items:
        logger.info(
            "extract_members: %d pages → Haiku, %d pages → Sonnet",
            len(haiku_items), len(sonnet_items),
        )

    # Phase 2 — batch LLM calls per model
    all_rows: list[dict] = []
    processed_pis: set[str] = set()

    for model, items, batch_size in (
        ("haiku", haiku_items, config.settings.haiku_batch_size),
        ("sonnet", sonnet_items, config.settings.sonnet_batch_size),
    ):
        if not items:
            continue
        texts = [item["text"] for item in items]
        for index_batch in llm.build_batches(texts, max_items=batch_size):
            batch = [items[i] for i in index_batch]
            extracted = _extract_batch(batch, model=model)

            for item in batch:
                pi = item["pi"]
                pi_name = item["pi_name"]
                processed_pis.add(pi_name)

                all_rows.append(_pi_row(pi))

                members = extracted.get(pi_name, [])
                if not isinstance(members, list):
                    logger.warning(
                        "Unexpected member list type for %s: %s", pi_name, type(members)
                    )
                    continue

                for member in members:
                    if not isinstance(member, dict) or not member.get("name"):
                        continue
                    if member["name"].strip().lower() == pi_name.lower():
                        continue
                    all_rows.append(_member_row(pi, member))

    # PIs with no member page still get a PI row
    for pi in pi_no_page:
        all_rows.append(_pi_row(pi))

    total_members = sum(1 for r in all_rows if r.get("member_role") != "PI")
    logger.info(
        "extract_members: %d PI rows + %d member rows = %d total",
        len(pis), total_members, len(all_rows),
    )
    return all_rows
