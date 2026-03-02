"""
Pipeline 2, Step 3: Fine-grained relevance scoring for individual researchers.

For each member under a candidate PI (those that passed the coarse filter):
  - Combines lab context (pi_name, lab_research_summary) with individual
    publication history from step2_scholar_lookup results (or from the sheet
    if already populated).
  - Batches researchers into groups of config.sonnet_batch_size and sends a
    single Sonnet call per batch.
  - Returns a flat list of scored dicts ready to write back to the sheet:
    {member_name, pi_name, institution, relevance_flag, relevance_reasoning}
"""
import json
import logging
import re

from src import llm
from src.config import Config
from src.pipeline1.step2_pi_extraction import chunks, extract_json_str
from src.pipeline2.step2_scholar_lookup import ScholarResult
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Name normalisation for identity dedup (mirrors step2_scholar_lookup)
# ---------------------------------------------------------------------------

_MIDDLE_INITIAL_RE = re.compile(r"(?<=\s)[a-z]\.\s*")


def _normalize_member_name(name: str) -> str:
    n = name.strip().lower()
    n = _MIDDLE_INITIAL_RE.sub("", n)
    return " ".join(n.split())

_SYSTEM = (
    "You assess whether individual researchers are relevant to a specific research "
    "topic based on their lab context and publication history. "
    "Be precise in your reasoning."
)

_NO_PAPERS_MSG = "No publication data available — assess based on lab context only."


# ---------------------------------------------------------------------------
# LLM batch call
# ---------------------------------------------------------------------------

def _score_batch(situation: str, batch: list[dict]) -> list[dict]:
    """
    Score a batch of researcher dicts for relevance to `situation`.

    Each item must have: member_name, pi_name, lab_research_summary, papers (list[str]).
    Returns a list of dicts: {member_name, pi_name, institution,
                               relevance_flag, relevance_reasoning}.
    """
    lines = []
    for i, m in enumerate(batch):
        papers_str = "; ".join(m["papers"]) if m.get("papers") else _NO_PAPERS_MSG
        lines.append(
            f"{i + 1}. {m['member_name']}\n"
            f"   Lab: {m['pi_name']} — {m['lab_research_summary']}\n"
            f"   Recent papers: {papers_str}"
        )

    user_prompt = (
        f'Situation of interest: "{situation}"\n\n'
        "For each researcher below, determine if their work is relevant to the "
        "situation of interest. Consider both their lab context and individual "
        "publications (if available).\n\n"
        + "\n\n".join(lines)
        + "\n\n"
        "For each numbered researcher, return a JSON object:\n"
        "{\n"
        '  "1": {"relevant": true,  "reasoning": "one sentence explaining why"},\n'
        '  "2": {"relevant": false, "reasoning": "one sentence explaining why"},\n'
        "  ...\n"
        "}"
    )

    response = llm.call_sonnet(prompt=user_prompt, system=_SYSTEM)
    json_str = extract_json_str(response)

    try:
        decisions = json.loads(json_str)
        if not isinstance(decisions, dict):
            raise ValueError(f"Expected dict, got {type(decisions).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "JSON parse error in _score_batch: %s | snippet: %.400s", exc, response
        )
        # Salvage: default everything to relevant=False with explanation
        decisions = {
            str(i + 1): {"relevant": False, "reasoning": "Parse error — could not determine relevance."}
            for i in range(len(batch))
        }

    results = []
    for i, m in enumerate(batch):
        entry = decisions.get(str(i + 1), {})
        if not isinstance(entry, dict):
            entry = {}

        # Coerce "relevant" to bool; accept string "true"/"false" defensively
        raw_relevant = entry.get("relevant", False)
        if isinstance(raw_relevant, str):
            relevant = raw_relevant.strip().lower() == "true"
        else:
            relevant = bool(raw_relevant)

        reasoning = str(entry.get("reasoning", "")).strip()
        if not reasoning:
            reasoning = "No reasoning provided."

        results.append({
            "member_name": m["member_name"],
            "pi_name": m["pi_name"],
            "institution": m["institution"],
            "relevance_flag": relevant,
            "relevance_reasoning": reasoning,
        })

    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def score_relevance(
    candidate_pis: list[str],
    scholar_results: dict[tuple[str, str], ScholarResult],
    situation: str,
    config: Config,
    tab_name: str = "Master List",
) -> list[dict]:
    """
    Score individual researchers for relevance to a situation of interest.

    Args:
        candidate_pis:   List of "{pi_name}|||{institution}" strings from step1.
        scholar_results: Dict[(pi_name, member_name) -> ScholarResult] from step2.
                         May be empty — the function falls back to sheet data.
        situation:       Free-text situation of interest.
        config:          Loaded Config.
        tab_name:        Sheet tab to read member rows from.

    Returns:
        List of dicts, one per member:
        {member_name, pi_name, institution, relevance_flag, relevance_reasoning}
    """
    # Parse candidate PI set
    candidate_set: set[tuple[str, str]] = set()
    for ident in candidate_pis:
        parts = ident.split("|||", 1)
        if len(parts) == 2:
            candidate_set.add((parts[0].strip(), parts[1].strip()))

    # Read sheet rows
    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)

    # Build a flat member list (one entry per sheet row)
    raw_members: list[dict] = []
    for row in all_rows:
        pi_name = row.get("pi_name", "").strip()
        institution = row.get("institution", "").strip()
        member_name = row.get("member_name", "").strip()

        if not member_name or not pi_name:
            continue
        if (pi_name, institution) not in candidate_set:
            continue

        # Resolve papers: step2 results take precedence; fall back to sheet
        scholar = scholar_results.get((pi_name, member_name))
        if scholar and scholar.papers:
            papers = scholar.papers
        else:
            raw = row.get("recent_papers", "").strip()
            papers = [p.strip() for p in raw.split(";") if p.strip()] if raw else []

        raw_members.append({
            "member_name": member_name,
            "pi_name": pi_name,
            "institution": institution,
            "lab_research_summary": row.get("lab_research_summary", "").strip(),
            "papers": papers,
        })

    if not raw_members:
        logger.warning("score_relevance: no members found for the given candidate PIs")
        return []

    # ------------------------------------------------------------------
    # Dedup by normalised (member_name, institution).
    # A person listed under N PIs is scored once with combined lab context;
    # the result fans back to all original (pi_name, member_name) rows.
    # ------------------------------------------------------------------
    identity_to_group: dict[tuple[str, str], list[dict]] = {}
    for m in raw_members:
        identity = (_normalize_member_name(m["member_name"]), m["institution"].lower())
        identity_to_group.setdefault(identity, []).append(m)

    total_rows = len(raw_members)
    unique_people = len(identity_to_group)
    logger.info(
        "Relevance scoring: %d row(s) → %d unique member(s) "
        "(%d duplicate scoring(s) avoided)",
        total_rows, unique_people, total_rows - unique_people,
    )

    # Build one scoring entry per unique person; for multi-PI people combine
    # all lab summaries so the LLM has the full picture.
    unique_members: list[dict] = []
    unique_identities: list[tuple[str, str]] = []

    for identity, group in identity_to_group.items():
        primary = group[0]
        if len(group) == 1:
            lab_summary = primary["lab_research_summary"]
        else:
            parts = [
                f"{m['pi_name']} — {m['lab_research_summary']}"
                for m in group
                if m["lab_research_summary"]
            ]
            lab_summary = "; also in: ".join(parts) if parts else ""

        unique_members.append({
            "member_name": primary["member_name"],
            "pi_name": primary["pi_name"],
            "institution": primary["institution"],
            "lab_research_summary": lab_summary,
            "papers": primary["papers"],
        })
        unique_identities.append(identity)

    # Score unique members
    batch_size = config.settings.sonnet_batch_size
    scored_unique: list[dict] = []
    for batch in chunks(unique_members, batch_size):
        scored_unique.extend(_score_batch(situation, batch))

    # Fan scored results back to ALL original (pi_name, member_name) rows
    all_scored: list[dict] = []
    for scored, identity in zip(scored_unique, unique_identities):
        for m in identity_to_group[identity]:
            all_scored.append({
                "member_name": m["member_name"],
                "pi_name": m["pi_name"],
                "institution": m["institution"],
                "relevance_flag": scored["relevance_flag"],
                "relevance_reasoning": scored["relevance_reasoning"],
            })

    relevant_count = sum(1 for r in all_scored if r["relevance_flag"])
    logger.info(
        "score_relevance: %d member(s) scored — %d relevant (%.0f%%), %d not relevant",
        len(all_scored),
        relevant_count,
        relevant_count / len(all_scored) * 100 if all_scored else 0,
        len(all_scored) - relevant_count,
    )

    return all_scored
