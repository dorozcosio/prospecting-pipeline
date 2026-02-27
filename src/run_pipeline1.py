"""
Pipeline 1: Institute-Based Scraping

Crawls university faculty/department pages to build a flat member list and
syncs it to the Master List Google Sheet incrementally (one group at a time).

Usage:
    python -m src.run_pipeline1 [--institution NAME] [--resume | --fresh]
                                [--dry-run] [--group-size N]

Flags:
    --institution NAME  Run for a single institution only (must match a name
                        in config/institutions.yaml). Defaults to all.
    --resume            Continue from an existing checkpoint, skipping PIs
                        that were already processed in a prior run.
    --fresh             Clear any existing checkpoint and start from scratch.
                        Required when a checkpoint exists and --resume is not given.
    --dry-run           Run all steps but do not write to the sheet.
                        Saves would-be rows to logs/p1_dry_run_{ts}.json.
    --group-size N      Number of PIs to process per incremental write batch
                        (default: 15).
"""
import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.checkpoint import clear_checkpoint, load_checkpoint, save_checkpoint
from src.config import load_config
from src.logging_config import setup_logging
from src.pipeline1.step1_url_discovery import discover_urls
from src.pipeline1.step2_pi_extraction import extract_pis
from src.pipeline1.step3_lab_homepages import find_lab_homepages
from src.pipeline1.step4_member_pages import find_member_pages
from src.pipeline1.step5_member_extraction import extract_members
from src.pipeline1.step6_sheet_writer import init_run_cache, write_pipeline1_results

logger = logging.getLogger(__name__)

_DEFAULT_GROUP_SIZE = 15


def run_pipeline1(
    dry_run: bool = False,
    institution: str | None = None,
    resume: bool = False,
    group_size: int = _DEFAULT_GROUP_SIZE,
    tab_name: str = "Master List",
    urls_override: dict | None = None,
) -> dict:
    """
    Execute all Pipeline 1 steps with incremental sheet writes.

    Phase 1: Discover URLs (Step 1) and extract PI lists (Step 2) for all
             target institutions.
    Phase 2: For each group of `group_size` PIs:
             - Find lab homepages (Step 3)
             - Find member pages (Step 4)
             - Extract lab members (Step 5)
             - Write to sheet (Step 6) — deactivation is scoped and deferred
               to a final pass after all groups complete.
             - Checkpoint completed PIs so the run can resume after a crash.

    Args:
        dry_run:       Skip the sheet write; save rows to logs/p1_dry_run_{ts}.json.
        institution:   Limit to one institution name (as in institutions.yaml).
        resume:        Start from an existing checkpoint.
        group_size:    Number of PIs processed per incremental write.
        tab_name:      Target sheet tab (default "Master List").
        urls_override: Optional {institution_name: [url_dicts]} to skip Step 1.
                       Useful for testing and re-runs without fresh URL discovery.

    Returns:
        {institutions_processed, pis_found, members_found, members, sheet_summary}
    """
    config = load_config()
    start_time = time.time()

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

    # Determine which institutions are in scope — used to scope deactivation
    # so that a run with --institution MIT never touches Harvard/Stanford rows.
    if institution:
        institutions_in_run = [institution]
    else:
        institutions_in_run = [inst.name for inst in all_institutions]

    # ------------------------------------------------------------------
    # Phase 1: URL discovery + PI extraction (Steps 1 & 2)
    # ------------------------------------------------------------------
    logger.info("=== Phase 1: Discovery ===")

    if urls_override is not None:
        all_urls = urls_override
        logger.info("Step 1: using %d URL dict(s) from override", len(all_urls))
    else:
        all_urls = discover_urls(config)
        if institution:
            all_urls = {k: v for k, v in all_urls.items() if k == institution}
        for inst_name, urls in all_urls.items():
            logger.info("Step 1: %s — %d URL(s) discovered", inst_name, len(urls))

    all_pis: list[dict] = []
    for inst_name, urls in all_urls.items():
        pis = extract_pis(inst_name, urls, config)
        logger.info("Step 2: %s — %d PI(s) extracted", inst_name, len(pis))
        all_pis.extend(pis)

    logger.info(
        "Phase 1 complete: %d PI(s) across %d institution(s)",
        len(all_pis), len(all_urls),
    )

    # ------------------------------------------------------------------
    # Phase 2: Enrichment in groups, write after each group
    # ------------------------------------------------------------------
    logger.info("=== Phase 2: Enrichment (group_size=%d) ===", group_size)

    checkpoint = load_checkpoint()
    completed_keys = set(checkpoint.get("completed_pis", []))

    remaining_pis = [
        pi for pi in all_pis
        if f"{pi['institution']}|||{pi['name']}" not in completed_keys
    ]
    already_done = len(all_pis) - len(remaining_pis)
    if already_done:
        logger.info(
            "Resuming: %d already done, %d remaining", already_done, len(remaining_pis)
        )

    if not dry_run:
        init_run_cache()

    all_members_written: list[dict] = []
    dry_run_rows: list[dict] = []

    for group_start in range(0, len(remaining_pis), group_size):
        group = remaining_pis[group_start : group_start + group_size]
        group_num = group_start // group_size + 1
        total_groups = (len(remaining_pis) + group_size - 1) // group_size

        logger.info(
            "\n--- Group %d/%d: PIs %d–%d ---",
            group_num, total_groups,
            group_start + 1, group_start + len(group),
        )

        # Steps 3–5
        group = find_lab_homepages(group, config)
        group = find_member_pages(group, config)
        group_members = extract_members(group, config)

        # Step 6: write this group (no deactivation during incremental writes)
        if dry_run:
            dry_run_rows.extend(group_members)
            logger.info("[DRY RUN] Group %d: %d rows buffered", group_num, len(group_members))
        else:
            result = write_pipeline1_results(
                group_members,
                config,
                institutions_in_run=institutions_in_run,
                tab_name=tab_name,
                deactivate_missing=False,
            )
            all_members_written.extend(group_members)
            logger.info("Group %d written: %s", group_num, result)

        # Checkpoint
        for pi in group:
            save_checkpoint(f"{pi['institution']}|||{pi['name']}")

        # Progress estimate
        pis_done = already_done + group_start + len(group)
        pis_total = len(all_pis)
        pis_remaining_count = pis_total - pis_done
        elapsed = time.time() - start_time
        if pis_done > 0:
            mins_per_pi = elapsed / 60 / pis_done
            est_remaining = mins_per_pi * pis_remaining_count
            logger.info(
                "PROGRESS: %d/%d PIs | Elapsed: %.1fmin | "
                "Est remaining: %.1fmin | Rate: %.2f PIs/min",
                pis_done, pis_total,
                elapsed / 60,
                est_remaining,
                1 / mins_per_pi if mins_per_pi else 0,
            )

    # ------------------------------------------------------------------
    # Final deactivation pass — scoped to institutions processed this run
    # ------------------------------------------------------------------
    if not dry_run and all_members_written:
        logger.info(
            "Final deactivation pass for institutions: %s", institutions_in_run
        )
        deactivation_result = write_pipeline1_results(
            all_members_written,
            config,
            institutions_in_run=institutions_in_run,
            tab_name=tab_name,
            deactivate_missing=True,
        )
        logger.info("Deactivation pass: %s", deactivation_result)

    # ------------------------------------------------------------------
    # Dry-run save
    # ------------------------------------------------------------------
    sheet_summary: dict = {}
    if dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dry_run_path = Path("logs") / f"p1_dry_run_{ts}.json"
        dry_run_path.parent.mkdir(exist_ok=True)
        with dry_run_path.open("w") as f:
            json.dump(dry_run_rows, f, indent=2, default=str)
        logger.info(
            "[DRY RUN] Would write %d rows — saved to %s",
            len(dry_run_rows), dry_run_path,
        )
        sheet_summary = {
            "added": 0, "updated": 0, "reactivated": 0,
            "deactivated": 0, "unchanged": 0, "dry_run": True,
        }

    # ------------------------------------------------------------------
    # Final console summary
    # ------------------------------------------------------------------
    all_members = dry_run_rows if dry_run else all_members_written
    pi_rows = sum(1 for m in all_members if m.get("member_role") == "PI")
    member_rows = sum(1 for m in all_members if m.get("member_role") != "PI")
    elapsed_total = time.time() - start_time

    print("\n=== Pipeline 1 Summary ===")
    print(f"  Institutions processed : {len(all_urls)}")
    print(f"  PIs found              : {len(all_pis)}")
    print(f"  Members found          : {member_rows}")
    print(f"  Total elapsed          : {elapsed_total / 60:.1f} min")
    if dry_run:
        print(f"  [DRY RUN] {len(all_members)} rows saved (not written to sheet)")
    else:
        print(f"  Rows added             : {sheet_summary.get('added', 0)}")
        print(f"  Rows updated           : {sheet_summary.get('updated', 0)}")
        print(f"  Rows deactivated       : {sheet_summary.get('deactivated', 0)}")
        print(f"  Rows unchanged         : {sheet_summary.get('unchanged', 0)}")

    return {
        "institutions_processed": len(all_urls),
        "pis_found": len(all_pis),
        "members_found": member_rows,
        "members": all_members,
        "sheet_summary": sheet_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline 1: Institute-Based Scraping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--institution",
        metavar="NAME",
        help="Run for a single institution only (e.g. 'MIT').",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from an existing checkpoint.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear any existing checkpoint and start from scratch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all steps but save output to JSON instead of writing to the sheet.",
    )
    parser.add_argument(
        "--group-size",
        type=int,
        default=_DEFAULT_GROUP_SIZE,
        metavar="N",
        help=f"PIs per incremental write batch (default: {_DEFAULT_GROUP_SIZE}).",
    )
    args = parser.parse_args()

    if args.resume and args.fresh:
        parser.error("--resume and --fresh are mutually exclusive")

    setup_logging()

    # Handle checkpoint state
    checkpoint = load_checkpoint()
    n_done = len(checkpoint.get("completed_pis", []))

    if args.fresh:
        if n_done:
            logger.info("--fresh: clearing checkpoint (%d completed PIs)", n_done)
        clear_checkpoint()
    elif not args.resume and n_done > 0:
        print(
            f"Found checkpoint with {n_done} PIs done. "
            "Use --resume to continue or --fresh to start over."
        )
        sys.exit(1)

    try:
        run_pipeline1(
            dry_run=args.dry_run,
            institution=args.institution,
            resume=args.resume or (n_done == 0),
            group_size=args.group_size,
        )
    except Exception:
        logger.error("Pipeline 1 failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
