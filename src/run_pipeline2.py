"""
Pipeline 2: Situation-Based Filtering

Reads the Master List sheet built by Pipeline 1, filters and enriches
researchers for a specific situation of interest, and writes scores, Scholar
data, and emails back to the sheet.

Usage:
    python -m src.run_pipeline2 --situation "description" [--dry-run] [--skip-scholar]

Flags:
    --situation TEXT    (Required) Free-text description of the situation of
                        interest used for coarse filtering and relevance scoring.
    --dry-run           Run all steps but do not write to the sheet.
                        Saves output to logs/p2_dry_run_{ts}.json.
    --skip-scholar      Skip Google Scholar lookups entirely. Useful for
                        quick re-scoring with a new situation when papers are
                        already populated in the sheet.
"""
import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.logging_config import setup_logging
from src.pipeline2.step1_coarse_filter import coarse_filter_pis
from src.pipeline2.step2_scholar_lookup import lookup_members
from src.pipeline2.step3_fine_scoring import score_relevance
from src.pipeline2.step4_email_lookup import lookup_emails
from src.pipeline2.step5_sheet_writer import (
    write_email_batch,
    write_pipeline2_results,
    write_relevance_batch,
    write_scholar_batch,
)

logger = logging.getLogger(__name__)


def run_pipeline2(
    situation: str,
    dry_run: bool = False,
    skip_scholar: bool = False,
    tab_name: str = "Master List",
) -> dict:
    """
    Execute all Pipeline 2 steps.

    Args:
        situation:    Free-text situation of interest (e.g. "CRISPR for cancer").
        dry_run:      If True, skip the sheet write and save output to JSON.
        skip_scholar: If True, skip Google Scholar lookups (step 2).
        tab_name:     Sheet tab to read/write (default "Master List").

    Returns:
        {passing_pis, members_scored, flagged_relevant, emails_found,
         relevance_scores, sheet_summary}
    """
    config = load_config()

    # ------------------------------------------------------------------
    # Step 1: Coarse filter
    # ------------------------------------------------------------------
    passing_pis = coarse_filter_pis(situation, config, tab_name=tab_name)
    logger.info(
        "Step 1: %d PI(s) pass coarse filter for situation: %r",
        len(passing_pis), situation,
    )

    # ------------------------------------------------------------------
    # Step 2: Scholar lookup (optional)
    #
    # When not dry_run: lookups fire an on_batch_complete callback every
    # scholar_flush_interval members.  Each callback immediately writes
    # Scholar data, scores that batch, looks up emails for flagged members,
    # and writes emails — so partial results reach the sheet quickly and
    # a crash only loses at most flush_interval lookups worth of work.
    # ------------------------------------------------------------------
    scholar_results: dict = {}

    # Shared state accumulated by the incremental-flush callback
    _flush_state: dict = {
        "scholar_results": {},   # all Scholar results seen so far
        "emails": {},            # all emails written so far
        "batch_num": 0,
    }

    def _flush_batch(batch: dict) -> None:
        """Callback fired every flush_interval Scholar lookups."""
        _flush_state["batch_num"] += 1
        batch_n = _flush_state["batch_num"]
        scholar_b = batch["scholar_results"]
        pi_ids_b = batch["pi_identifiers"]

        logger.info(
            "=== Incremental flush — batch %d: %d Scholar result(s) for %d PI(s) ===",
            batch_n, len(scholar_b), len(set(pi_ids_b)),
        )

        # a. Persist Scholar data to the sheet
        n_written = write_scholar_batch(scholar_b, config, tab_name=tab_name)
        _flush_state["scholar_results"].update(scholar_b)

        # b. Score this batch's members (uses just-written Scholar data)
        batch_scores = score_relevance(
            pi_ids_b, scholar_b, situation, config, tab_name=tab_name
        )
        write_relevance_batch(batch_scores, situation, config, tab_name=tab_name)

        # c. Email lookup for flagged members in this batch
        flagged_b = [s for s in batch_scores if s["relevance_flag"]]
        batch_emails: dict = {}
        if flagged_b:
            batch_emails = lookup_emails(flagged_b, config)
            write_email_batch(batch_emails, config, tab_name=tab_name)
            _flush_state["emails"].update(batch_emails)

        logger.info(
            "Batch %d flush complete: %d Scholar written, %d scored, "
            "%d relevant, %d emails written",
            batch_n, n_written, len(batch_scores),
            len(flagged_b),
            sum(1 for e in batch_emails.values() if e),
        )

    if skip_scholar:
        logger.info("Step 2: Scholar lookup skipped (--skip-scholar)")
    else:
        scholar_results = lookup_members(
            passing_pis,
            config,
            tab_name=tab_name,
            on_batch_complete=_flush_batch if not dry_run else None,
        )
        found = sum(1 for r in scholar_results.values() if r.status == "found")
        ambig = sum(1 for r in scholar_results.values() if r.status == "ambiguous")
        not_found = sum(1 for r in scholar_results.values() if r.status == "not_found")
        errors = sum(1 for r in scholar_results.values() if r.status == "error")
        logger.info(
            "Step 2: %d lookups — %d found, %d ambiguous, %d not_found, %d errors",
            len(scholar_results), found, ambig, not_found, errors,
        )

    # ------------------------------------------------------------------
    # Step 3: Final relevance scoring — ALL candidate members
    #
    # When incremental flush ran (not skip_scholar, not dry_run), per-batch
    # scores already give early visibility, but we re-score the full candidate
    # set here for consistency and to catch any cross-batch differences.
    # All Scholar data from the incremental passes is used.
    # ------------------------------------------------------------------
    _all_scholar = _flush_state["scholar_results"] if not skip_scholar and not dry_run else scholar_results
    if not skip_scholar and not dry_run:
        logger.info(
            "Step 3: Final reconciliation — scoring ALL %d candidate PI(s)", len(passing_pis)
        )
    relevance_scores = score_relevance(
        passing_pis, _all_scholar, situation, config, tab_name=tab_name
    )
    flagged = sum(1 for r in relevance_scores if r["relevance_flag"])
    logger.info(
        "Step 3: %d member(s) scored — %d flagged relevant (%.0f%%)",
        len(relevance_scores),
        flagged,
        flagged / len(relevance_scores) * 100 if relevance_scores else 0,
    )

    # ------------------------------------------------------------------
    # Step 4: Email lookup
    #
    # Incremental path: emails were written per batch; only look up emails
    # for any members who became relevant in the final reconciliation but
    # were not already covered by a batch pass.
    # Original path (skip_scholar or dry_run): look up all flagged members.
    # ------------------------------------------------------------------
    flagged_members = [r for r in relevance_scores if r["relevance_flag"]]
    new_emails: dict = {}
    if not skip_scholar and not dry_run:
        already_looked_up = set(_flush_state["emails"].keys())
        new_flagged = [
            m for m in flagged_members
            if (m["pi_name"], m["member_name"]) not in already_looked_up
        ]
        new_emails = lookup_emails(new_flagged, config) if new_flagged else {}
        emails = {**_flush_state["emails"], **new_emails}
    else:
        emails = lookup_emails(flagged_members, config)

    emails_found = sum(1 for e in emails.values() if e)
    logger.info("Step 4: %d email(s) found for %d flagged member(s)", emails_found, len(flagged_members))

    # ------------------------------------------------------------------
    # Step 5: Write to sheet (or dry run)
    #
    # Incremental path: Scholar data and per-batch emails were already
    # written; pass empty dicts for those to avoid redundant updates.
    # The final write handles relevance scores for ALL active rows and
    # any new emails found in Step 4 above.
    # ------------------------------------------------------------------
    sheet_summary: dict = {}

    if dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dry_run_path = Path("logs") / f"p2_dry_run_{ts}.json"
        dry_run_path.parent.mkdir(exist_ok=True)
        output = {
            "situation": situation,
            "passing_pis": passing_pis,
            "relevance_scores": relevance_scores,
            "emails": {f"{k[0]}|||{k[1]}": v for k, v in emails.items()},
        }
        with dry_run_path.open("w") as f:
            json.dump(output, f, indent=2, default=str)
        logger.info(
            "[DRY RUN] Would write %d relevance score(s) — saved to %s",
            len(relevance_scores), dry_run_path,
        )
        sheet_summary = {"rows_scored": len(relevance_scores), "dry_run": True}
    elif not skip_scholar:
        # Incremental path: Scholar data already written per batch.
        # Final pass: write remaining new emails + full relevance reconciliation.
        if new_emails:
            write_email_batch(new_emails, config, tab_name=tab_name)
        sheet_summary = write_pipeline2_results(
            relevance_scores, {}, {}, situation, config, tab_name=tab_name
        )
    else:
        # skip_scholar path: write everything in one pass (original behaviour).
        sheet_summary = write_pipeline2_results(
            relevance_scores, scholar_results, emails, situation, config, tab_name=tab_name
        )

    # ------------------------------------------------------------------
    # Final console summary
    # ------------------------------------------------------------------
    print("\n=== Pipeline 2 Summary ===")
    print(f"  Situation         : {situation}")
    print(f"  PIs passing coarse: {len(passing_pis)}")
    print(f"  Members scored    : {len(relevance_scores)}")
    print(f"  Flagged relevant  : {flagged}")
    print(f"  Emails found      : {emails_found}")
    if dry_run:
        print(f"  [DRY RUN] Output saved — not written to sheet")

    # Per-institution breakdown — read updated sheet state (or use dry-run scores)
    if not dry_run:
        from src.sheets import SheetsClient
        _client = SheetsClient(config)
        _all_rows = _client.read_all_rows(tab_name)
    else:
        # In dry-run mode, approximate from in-memory relevance scores only
        _all_rows = relevance_scores

    unique_institutions = sorted({
        r.get("institution", "") for r in _all_rows if r.get("institution")
    })
    if unique_institutions:
        print("\n=== Results by Institution ===")
        for inst in unique_institutions:
            inst_rows = [r for r in _all_rows if r.get("institution") == inst]
            active = [
                r for r in inst_rows
                if r.get("status", "active") not in ("inactive",)
            ]
            relevant = [
                r for r in active
                if str(r.get("relevance_flag", "")).upper() in ("TRUE", "1") or
                r.get("relevance_flag") is True
            ]
            last_scraped = max(
                (r.get("institution_last_scraped", "") for r in inst_rows if r.get("institution_last_scraped")),
                default="unknown",
            )
            print(
                f"  {inst}: {len(relevant)} relevant / {len(active)} active "
                f"(last scraped: {last_scraped})"
            )

    return {
        "passing_pis": passing_pis,
        "members_scored": len(relevance_scores),
        "flagged_relevant": flagged,
        "emails_found": emails_found,
        "relevance_scores": relevance_scores,
        "sheet_summary": sheet_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline 2: Situation-Based Filtering",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--situation",
        required=True,
        metavar="TEXT",
        help="Free-text description of the situation of interest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all steps but save output to JSON instead of writing to the sheet.",
    )
    parser.add_argument(
        "--skip-scholar",
        action="store_true",
        help="Skip Google Scholar lookups (useful for quick re-scoring).",
    )
    args = parser.parse_args()

    setup_logging()

    try:
        run_pipeline2(
            situation=args.situation,
            dry_run=args.dry_run,
            skip_scholar=args.skip_scholar,
        )
    except Exception:
        logger.error("Pipeline 2 failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
