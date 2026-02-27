"""
Pipeline 1: Institute-Based Scraping

Crawls university faculty/department pages to build a flat member list and
syncs it to the Master List Google Sheet.

Usage:
    python -m src.run_pipeline1 [--dry-run] [--institution NAME]

Flags:
    --dry-run           Run all steps but do not write to the sheet.
                        Saves the would-be rows to logs/p1_dry_run_{ts}.json.
    --institution NAME  Run for a single institution only (must match a name
                        in config/institutions.yaml). Defaults to all.
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
from src.pipeline1.step1_url_discovery import discover_urls
from src.pipeline1.step2_pi_extraction import extract_pis
from src.pipeline1.step3_lab_homepages import find_lab_homepages
from src.pipeline1.step4_member_pages import find_member_pages
from src.pipeline1.step5_member_extraction import extract_members
from src.pipeline1.step6_sheet_writer import write_pipeline1_results

logger = logging.getLogger(__name__)


def run_pipeline1(
    dry_run: bool = False,
    institution: str | None = None,
    urls_override: dict | None = None,
    tab_name: str = "Master List",
) -> dict:
    """
    Execute all Pipeline 1 steps.

    Args:
        dry_run:        If True, skip the sheet write and save rows to a JSON file.
        institution:    Limit run to this institution name (as in institutions.yaml).
        urls_override:  Optional {institution_name: [url_dicts]} to skip step 1.
                        Useful for testing and re-runs without fresh URL discovery.
        tab_name:       Sheet tab to write results to (default "Master List").

    Returns:
        {institutions_processed, pis_found, members_found, members, sheet_summary}
    """
    config = load_config()

    # ------------------------------------------------------------------
    # Resolve institutions to process
    # ------------------------------------------------------------------
    all_institutions = config.institutions
    if institution:
        all_institutions = [i for i in all_institutions if i.name == institution]
        if not all_institutions:
            raise ValueError(
                f"Institution '{institution}' not found in config. "
                f"Available: {[i.name for i in config.institutions]}"
            )

    # ------------------------------------------------------------------
    # Step 1: URL discovery (or use override)
    # ------------------------------------------------------------------
    if urls_override is not None:
        all_urls = urls_override
        logger.info("Step 1: using %d URL dict(s) from override", len(all_urls))
    else:
        all_urls = discover_urls(config)
        if institution:
            all_urls = {k: v for k, v in all_urls.items() if k == institution}

    for inst_name, urls in all_urls.items():
        logger.info("Step 1: %s — %d URL(s) discovered", inst_name, len(urls))

    # ------------------------------------------------------------------
    # Step 2: Extract PIs
    # ------------------------------------------------------------------
    all_pis: list[dict] = []
    for inst_name, urls in all_urls.items():
        pis = extract_pis(inst_name, urls, config)
        logger.info("Step 2: %s — %d PI(s) extracted", inst_name, len(pis))
        all_pis.extend(pis)

    logger.info("Step 2 total: %d PI(s) across %d institution(s)", len(all_pis), len(all_urls))

    # ------------------------------------------------------------------
    # Step 3: Find lab homepages
    # ------------------------------------------------------------------
    all_pis = find_lab_homepages(all_pis, config)
    found_homepages = sum(1 for p in all_pis if p.get("lab_homepage_url"))
    missing_homepages = len(all_pis) - found_homepages
    logger.info(
        "Step 3: %d/%d PIs have a homepage (%d missing)",
        found_homepages, len(all_pis), missing_homepages,
    )

    # ------------------------------------------------------------------
    # Step 4: Find member pages
    # ------------------------------------------------------------------
    all_pis = find_member_pages(all_pis, config)
    found_member_pages = sum(1 for p in all_pis if p.get("lab_members_url"))
    missing_member_pages = len(all_pis) - found_member_pages
    logger.info(
        "Step 4: %d/%d PIs have a members page (%d missing)",
        found_member_pages, len(all_pis), missing_member_pages,
    )

    # ------------------------------------------------------------------
    # Step 5: Extract members
    # ------------------------------------------------------------------
    members = extract_members(all_pis, config)
    pi_rows = sum(1 for m in members if m.get("member_role") == "PI")
    member_rows = sum(1 for m in members if m.get("member_role") != "PI")
    logger.info(
        "Step 5: %d PI rows + %d member rows = %d total",
        pi_rows, member_rows, len(members),
    )

    # ------------------------------------------------------------------
    # Step 6: Write to sheet (or dry run)
    # ------------------------------------------------------------------
    sheet_summary: dict = {}

    if dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dry_run_path = Path("logs") / f"p1_dry_run_{ts}.json"
        dry_run_path.parent.mkdir(exist_ok=True)
        with dry_run_path.open("w") as f:
            json.dump(members, f, indent=2, default=str)
        logger.info(
            "[DRY RUN] Would write %d rows — saved to %s", len(members), dry_run_path
        )
        sheet_summary = {
            "added": 0, "updated": 0, "deactivated": 0, "unchanged": 0, "dry_run": True
        }
    else:
        sheet_summary = write_pipeline1_results(members, config, tab_name=tab_name)

    # ------------------------------------------------------------------
    # Final console summary
    # ------------------------------------------------------------------
    print("\n=== Pipeline 1 Summary ===")
    print(f"  Institutions processed : {len(all_urls)}")
    print(f"  PIs found              : {len(all_pis)}")
    print(f"  Members found          : {member_rows}")
    if dry_run:
        print(f"  [DRY RUN] {len(members)} rows saved (not written to sheet)")
    else:
        print(f"  Rows added             : {sheet_summary.get('added', 0)}")
        print(f"  Rows updated           : {sheet_summary.get('updated', 0)}")
        print(f"  Rows deactivated       : {sheet_summary.get('deactivated', 0)}")
        print(f"  Rows unchanged         : {sheet_summary.get('unchanged', 0)}")

    return {
        "institutions_processed": len(all_urls),
        "pis_found": len(all_pis),
        "members_found": member_rows,
        "members": members,
        "sheet_summary": sheet_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline 1: Institute-Based Scraping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all steps but save output to JSON instead of writing to the sheet.",
    )
    parser.add_argument(
        "--institution",
        metavar="NAME",
        help="Run for a single institution only (e.g. 'MIT').",
    )
    args = parser.parse_args()

    setup_logging()

    try:
        run_pipeline1(dry_run=args.dry_run, institution=args.institution)
    except Exception:
        logger.error("Pipeline 1 failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
