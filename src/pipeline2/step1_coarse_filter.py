"""
Pipeline 2, Step 1: Coarse relevance filter for PIs.

For a given situation of interest:
  1. Read all active PI rows from the Master List tab.
  2. Deduplicate by (institution, pi_name), retaining lab_research_summary.
  3. Batch PIs (haiku_batch_size per call) and ask Haiku YES/NO per lab.
  4. Return identifiers of passing PIs as "{pi_name}|||{institution}" strings.
"""
import json
import logging

from src import llm
from src.config import Config
from src.pipeline1.step2_pi_extraction import chunks, extract_json_str
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You screen research labs for relevance to a given topic. "
    "Err on the side of inclusion — answer YES if there's any plausible connection."
)


# ---------------------------------------------------------------------------
# LLM batch call
# ---------------------------------------------------------------------------

def _screen_batch(situation: str, batch: list[dict]) -> list[str]:
    """
    Ask Haiku which labs in `batch` are relevant to `situation`.

    Each item in batch: {pi_name, institution, lab_research_summary}.
    Returns a list of "{pi_name}|||{institution}" for each YES.
    """
    lines = [
        f"{i + 1}. {item['pi_name']} ({item['institution']}): {item['lab_research_summary']}"
        for i, item in enumerate(batch)
    ]
    user_prompt = (
        f'Situation of interest: "{situation}"\n\n'
        "For each lab below, answer YES if the lab could plausibly contain researchers "
        "relevant to the situation of interest, or NO if it's clearly unrelated.\n\n"
        + "\n".join(lines)
        + "\n\n"
        'Return ONLY a JSON object mapping each number to "YES" or "NO". '
        'Example: {"1": "YES", "2": "NO", "3": "YES"}'
    )

    response = llm.call_haiku(prompt=user_prompt, system=_SYSTEM)
    json_str = extract_json_str(response)

    try:
        decisions = json.loads(json_str)
        if not isinstance(decisions, dict):
            raise ValueError(f"Expected dict, got {type(decisions).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "JSON parse error in _screen_batch: %s | snippet: %.300s", exc, response
        )
        # Default: include all on parse failure to avoid false negatives
        return [f"{item['pi_name']}|||{item['institution']}" for item in batch]

    passing = []
    for i, item in enumerate(batch):
        verdict = decisions.get(str(i + 1), "").strip().upper()
        if verdict == "YES":
            passing.append(f"{item['pi_name']}|||{item['institution']}")
        elif verdict != "NO":
            # Ambiguous — err on the side of inclusion
            logger.debug(
                "Ambiguous verdict %r for %s — including", verdict, item["pi_name"]
            )
            passing.append(f"{item['pi_name']}|||{item['institution']}")

    return passing


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def coarse_filter_pis(
    situation: str,
    config: Config,
    tab_name: str = "Master List",
) -> list[str]:
    """
    Screen all active PIs in the sheet for relevance to a situation of interest.

    Args:
        situation: Free-text description of the situation (e.g. "CRISPR gene editing
                   for cancer therapy").
        config:    Loaded Config.
        tab_name:  Sheet tab to read from (default "Master List").

    Returns:
        List of "{pi_name}|||{institution}" strings for PIs that passed.
    """
    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)

    # Deduplicate active PIs by (institution, pi_name)
    seen: set[tuple[str, str]] = set()
    pi_list: list[dict] = []
    for row in all_rows:
        if row.get("status", "").strip().lower() != "active":
            continue
        key = (row.get("institution", "").strip(), row.get("pi_name", "").strip())
        if key in seen or not key[1]:
            continue
        seen.add(key)
        pi_list.append({
            "institution": key[0],
            "pi_name": key[1],
            "lab_research_summary": row.get("lab_research_summary", "").strip(),
        })

    if not pi_list:
        logger.warning("coarse_filter_pis: no active PIs found in '%s'", tab_name)
        return []

    batch_size = config.settings.haiku_batch_size
    passing: list[str] = []

    for batch in chunks(pi_list, batch_size):
        passing.extend(_screen_batch(situation, batch))

    filtered_out = len(pi_list) - len(passing)
    pass_rate = len(passing) / len(pi_list) * 100 if pi_list else 0
    logger.info(
        "coarse_filter_pis: %d PIs screened — %d passing (%.0f%%), %d filtered out",
        len(pi_list), len(passing), pass_rate, filtered_out,
    )

    return passing
