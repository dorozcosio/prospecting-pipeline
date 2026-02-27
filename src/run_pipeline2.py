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
from src.pipeline2.step5_sheet_writer import write_pipeline2_results

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
    # ------------------------------------------------------------------
    scholar_results: dict = {}
    if skip_scholar:
        logger.info("Step 2: Scholar lookup skipped (--skip-scholar)")
    else:
        scholar_results = lookup_members(passing_pis, config, tab_name=tab_name)
        found = sum(1 for r in scholar_results.values() if r.status == "found")
        ambig = sum(1 for r in scholar_results.values() if r.status == "ambiguous")
        not_found = sum(1 for r in scholar_results.values() if r.status == "not_found")
        errors = sum(1 for r in scholar_results.values() if r.status == "error")
        logger.info(
            "Step 2: %d lookups — %d found, %d ambiguous, %d not_found, %d errors",
            len(scholar_results), found, ambig, not_found, errors,
        )

    # ------------------------------------------------------------------
    # Step 3: Fine relevance scoring
    # ------------------------------------------------------------------
    relevance_scores = score_relevance(
        passing_pis, scholar_results, situation, config, tab_name=tab_name
    )
    flagged = sum(1 for r in relevance_scores if r["relevance_flag"])
    logger.info(
        "Step 3: %d member(s) scored — %d flagged relevant (%.0f%%)",
        len(relevance_scores),
        flagged,
        flagged / len(relevance_scores) * 100 if relevance_scores else 0,
    )

    # ------------------------------------------------------------------
    # Step 4: Email lookup (relevant members only)
    # ------------------------------------------------------------------
    flagged_members = [r for r in relevance_scores if r["relevance_flag"]]
    emails = lookup_emails(flagged_members, config)
    emails_found = sum(1 for e in emails.values() if e)
    logger.info("Step 4: %d email(s) found for %d flagged member(s)", emails_found, len(flagged_members))

    # ------------------------------------------------------------------
    # Step 5: Write to sheet (or dry run)
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
    else:
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
