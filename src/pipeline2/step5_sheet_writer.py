"""
Pipeline 2, Step 5: Write enrichment results back to the Master List.

Update rules per sheet row:

  Relevance columns — ALWAYS overwritten on every run:
    relevance_flag       ← from relevance_scores, or False if not scored
    relevance_reasoning  ← from relevance_scores, or ""
    situation_of_interest← the current situation string (always set)

  Scholar columns — only if new lookup data exists for this member:
    recent_papers        ← "; ".join(scholar.papers)
    scholar_lookup_status← scholar.status
    last_enriched        ← ISO timestamp of this run

  Email — append-only:
    email                ← written only when sheet value is blank AND
                           a non-empty email was found in this run.
                           An existing email is NEVER cleared.

  last_enriched is also updated when email is written for the first time.

Composite key: (institution, pi_name, member_name) — matched exactly as
stored in the sheet (no normalisation, to stay consistent with Pipeline 1).
"""
import logging
from datetime import datetime, timezone

from src.config import Config
from src.pipeline2.step2_scholar_lookup import ScholarResult
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

_KEY_COLS = ["institution", "pi_name", "member_name"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_pipeline2_results(
    relevance_scores: list[dict],
    scholar_results: dict[tuple[str, str], ScholarResult],
    emails: dict[tuple[str, str], str],
    situation: str,
    config: Config,
    tab_name: str = "Master List",
) -> dict:
    """
    Merge Pipeline 2 enrichment data into the sheet.

    Args:
        relevance_scores: Output of step3_fine_scoring — list of
                          {pi_name, member_name, institution,
                           relevance_flag, relevance_reasoning}.
        scholar_results:  Output of step2_scholar_lookup — dict keyed by
                          (pi_name, member_name) → ScholarResult.
        emails:           Output of step4_email_lookup — dict keyed by
                          (pi_name, member_name) → email string.
        situation:        The current situation-of-interest string.
        config:           Loaded Config.
        tab_name:         Sheet tab to update (default "Master List").

    Returns:
        {rows_scored, flagged_relevant, scholar_lookups_performed, emails_found}
    """
    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)

    if not all_rows:
        logger.warning("write_pipeline2_results: tab '%s' is empty", tab_name)
        return {
            "rows_scored": 0,
            "flagged_relevant": 0,
            "scholar_lookups_performed": 0,
            "emails_found": 0,
        }

    # Index relevance scores by (pi_name, member_name)
    score_index: dict[tuple[str, str], dict] = {
        (r["pi_name"], r["member_name"]): r
        for r in relevance_scores
    }

    now = _now_iso()

    updates: list[dict] = []
    rows_scored = 0
    flagged_relevant = 0
    scholar_lookups_performed = 0
    emails_found = 0

    for row in all_rows:
        pi_name = row.get("pi_name", "")
        member_name = row.get("member_name", "")
        institution = row.get("institution", "")

        if not member_name or not pi_name:
            continue

        key = (pi_name, member_name)
        update: dict = {
            "institution": institution,
            "pi_name": pi_name,
            "member_name": member_name,
        }
        enriched_this_row = False

        # ----------------------------------------------------------------
        # a. Relevance columns — always overwrite
        # ----------------------------------------------------------------
        score = score_index.get(key)
        if score is not None:
            update["relevance_flag"] = score["relevance_flag"]
            update["relevance_reasoning"] = score["relevance_reasoning"]
            rows_scored += 1
            if score["relevance_flag"]:
                flagged_relevant += 1
        else:
            # Not scored (PI didn't pass coarse filter) — still write defaults
            update["relevance_flag"] = False
            update["relevance_reasoning"] = ""

        update["situation_of_interest"] = situation

        # ----------------------------------------------------------------
        # b. Scholar columns — only if new data for this member
        # ----------------------------------------------------------------
        scholar = scholar_results.get(key)
        if scholar is not None:
            update["recent_papers"] = "; ".join(scholar.papers) if scholar.papers else ""
            update["scholar_lookup_status"] = scholar.status
            scholar_lookups_performed += 1
            enriched_this_row = True

        # ----------------------------------------------------------------
        # c. Email — append-only
        # ----------------------------------------------------------------
        found_email = emails.get(key, "")
        existing_email = row.get("email", "").strip()
        if found_email and not existing_email:
            update["email"] = found_email
            emails_found += 1
            enriched_this_row = True

        # ----------------------------------------------------------------
        # last_enriched — set when any enrichment happened
        # ----------------------------------------------------------------
        if enriched_this_row:
            update["last_enriched"] = now

        updates.append(update)

    # Flush all updates in one batched call
    if updates:
        client.update_rows(tab_name, updates, _KEY_COLS)

    summary = {
        "rows_scored": rows_scored,
        "flagged_relevant": flagged_relevant,
        "scholar_lookups_performed": scholar_lookups_performed,
        "emails_found": emails_found,
    }
    logger.info(
        "write_pipeline2_results '%s': %d scored, %d relevant, "
        "%d scholar lookups written, %d emails written",
        tab_name,
        rows_scored,
        flagged_relevant,
        scholar_lookups_performed,
        emails_found,
    )
    return summary
