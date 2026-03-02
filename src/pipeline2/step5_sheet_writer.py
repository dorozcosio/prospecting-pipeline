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

Single-responsibility batch writers (for incremental-flush use):
  write_scholar_batch()    — flush Scholar results for N members mid-run
  write_relevance_batch()  — flush relevance scores for N members mid-run
  write_email_batch()      — flush emails for N relevant members mid-run
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


# ---------------------------------------------------------------------------
# Single-responsibility batch writers (incremental flush)
# ---------------------------------------------------------------------------

def write_scholar_batch(
    scholar_results: dict[tuple[str, str], ScholarResult],
    config: Config,
    tab_name: str = "Master List",
) -> int:
    """
    Write Scholar enrichment data for a batch of members.

    Only updates rows that appear in `scholar_results`; all other rows are
    left untouched.  Updates: recent_papers, scholar_lookup_status, last_enriched.

    Args:
        scholar_results: Dict keyed by (pi_name, member_name) → ScholarResult.
        config:          Loaded Config.
        tab_name:        Sheet tab to update.

    Returns:
        Number of rows written.
    """
    if not scholar_results:
        return 0

    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)
    now = _now_iso()
    updates: list[dict] = []

    for row in all_rows:
        pi_name = row.get("pi_name", "")
        member_name = row.get("member_name", "")
        institution = row.get("institution", "")
        if not member_name or not pi_name:
            continue
        scholar = scholar_results.get((pi_name, member_name))
        if scholar is None:
            continue
        updates.append({
            "institution": institution,
            "pi_name": pi_name,
            "member_name": member_name,
            "recent_papers": "; ".join(scholar.papers) if scholar.papers else "",
            "scholar_lookup_status": scholar.status,
            "last_enriched": now,
        })

    if updates:
        client.update_rows(tab_name, updates, _KEY_COLS)

    logger.info(
        "write_scholar_batch '%s': %d row(s) written", tab_name, len(updates)
    )
    return len(updates)


def write_relevance_batch(
    scores: list[dict],
    situation: str,
    config: Config,
    tab_name: str = "Master List",
) -> int:
    """
    Write relevance scores for a batch of members.

    Overwrites relevance_flag, relevance_reasoning, and situation_of_interest
    for every row in `scores`.  Other columns are not touched.

    Args:
        scores:    List of {pi_name, member_name, institution,
                            relevance_flag, relevance_reasoning}.
        situation: The current situation-of-interest string.
        config:    Loaded Config.
        tab_name:  Sheet tab to update.

    Returns:
        Number of rows written.
    """
    if not scores:
        return 0

    updates = [
        {
            "institution": s.get("institution", ""),
            "pi_name": s["pi_name"],
            "member_name": s["member_name"],
            "relevance_flag": s["relevance_flag"],
            "relevance_reasoning": s["relevance_reasoning"],
            "situation_of_interest": situation,
        }
        for s in scores
    ]

    client = SheetsClient(config)
    client.update_rows(tab_name, updates, _KEY_COLS)

    flagged = sum(1 for s in scores if s["relevance_flag"])
    logger.info(
        "write_relevance_batch '%s': %d row(s) written — %d relevant",
        tab_name, len(updates), flagged,
    )
    return len(updates)


def write_email_batch(
    emails: dict[tuple[str, str], str],
    config: Config,
    tab_name: str = "Master List",
) -> int:
    """
    Write emails for a batch of members (append-only).

    An email is written only when the sheet row currently has no email AND
    the lookup produced a non-empty result.  Existing emails are never cleared.

    Args:
        emails:   Dict keyed by (pi_name, member_name) → email string.
        config:   Loaded Config.
        tab_name: Sheet tab to update.

    Returns:
        Number of emails written.
    """
    if not emails:
        return 0

    client = SheetsClient(config)
    all_rows = client.read_all_rows(tab_name)
    now = _now_iso()
    updates: list[dict] = []

    for row in all_rows:
        pi_name = row.get("pi_name", "")
        member_name = row.get("member_name", "")
        institution = row.get("institution", "")
        if not member_name or not pi_name:
            continue
        found_email = emails.get((pi_name, member_name), "")
        existing_email = row.get("email", "").strip()
        if found_email and not existing_email:
            updates.append({
                "institution": institution,
                "pi_name": pi_name,
                "member_name": member_name,
                "email": found_email,
                "last_enriched": now,
            })

    if updates:
        client.update_rows(tab_name, updates, _KEY_COLS)

    logger.info(
        "write_email_batch '%s': %d email(s) written", tab_name, len(updates)
    )
    return len(updates)


# ---------------------------------------------------------------------------
# Combined writer (used by dry-run path and final reconciliation pass)
# ---------------------------------------------------------------------------

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

        # Only score active rows — inactive rows were removed from source pages
        # and should not receive a fresh relevance assessment.
        if row.get("status", "").strip().lower() == "inactive":
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
            # PI didn't pass coarse filter — still write a clear not-relevant
            # assessment so every active row has a current relevance reading.
            update["relevance_flag"] = False
            update["relevance_reasoning"] = "PI lab not relevant to current situation"

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
