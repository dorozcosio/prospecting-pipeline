"""
Targeted Lab Member Re-Discovery + P2 Enrichment CLI.

Usage examples:

  # Single PI from command-line flags:
  python -m src.run_rescrape \\
      --pi "Jane Smith" --institution "MIT" \\
      --situation "machine learning for drug discovery"

  # Multiple PIs from a YAML file:
  python -m src.run_rescrape \\
      --pi-file pis_to_rescrape.yaml \\
      --situation "AI for clinical outcomes"

  # Multiple PIs via repeated flags:
  python -m src.run_rescrape \\
      --pi "Jane Smith" --institution "MIT" \\
      --pi "John Doe" --institution "Harvard" \\
      --situation "topic"

  # Dry run (no sheet writes, saves JSON to logs/):
  python -m src.run_rescrape \\
      --pi-file pis.yaml --situation "topic" --dry-run

YAML file format:
  - pi_name: Jane Smith
    institution: MIT
  - pi_name: John Doe
    institution: Harvard
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

from src.config import load_config
from src.pipeline_rescrape import rescrape_and_enrich

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Targeted Lab Member Re-Discovery + P2 Enrichment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # PI input — at least one of --pi-file or (--pi + --institution) required
    parser.add_argument(
        "--pi-file",
        metavar="PATH",
        help="YAML file listing PIs to rescrape. "
             "Format: list of {pi_name, institution} dicts.",
    )
    parser.add_argument(
        "--pi",
        dest="pi_names",
        action="append",
        metavar="NAME",
        default=[],
        help="PI name (may be repeated; must match --institution in order).",
    )
    parser.add_argument(
        "--institution",
        dest="institutions",
        action="append",
        metavar="INST",
        default=[],
        help="Institution for the corresponding --pi (may be repeated).",
    )

    # Required
    parser.add_argument(
        "--situation",
        required=True,
        metavar="TEXT",
        help="Free-text situation of interest used for P2 relevance scoring.",
    )

    # Options
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Skip all sheet writes; save output to logs/rescrape_dry_run_*.json.",
    )
    parser.add_argument(
        "--skip-scholar",
        action="store_true",
        default=False,
        help="Skip Google Scholar lookups (use existing sheet data).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Reserved for future cache-busting support.",
    )
    parser.add_argument(
        "--tab",
        default="Master List",
        metavar="TAB_NAME",
        help="Sheet tab to read/write (default: 'Master List').",
    )

    return parser.parse_args(argv)


def _load_pis(args) -> list[dict]:
    """Build the list of {pi_name, institution} dicts from CLI args."""
    pis: list[dict] = []

    # From --pi-file
    if args.pi_file:
        path = Path(args.pi_file)
        if not path.exists():
            logger.error("--pi-file not found: %s", path)
            sys.exit(1)
        with path.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, list):
            logger.error(
                "--pi-file must be a YAML list of {pi_name, institution} dicts"
            )
            sys.exit(1)
        for item in data:
            if not isinstance(item, dict):
                logger.error("Invalid entry in --pi-file: %r", item)
                sys.exit(1)
            if "pi_name" not in item or "institution" not in item:
                logger.error(
                    "Each entry in --pi-file must have 'pi_name' and 'institution': %r",
                    item,
                )
                sys.exit(1)
            pis.append({"pi_name": item["pi_name"], "institution": item["institution"]})

    # From --pi / --institution flags
    if args.pi_names or args.institutions:
        if len(args.pi_names) != len(args.institutions):
            logger.error(
                "--pi and --institution must be paired: got %d PI name(s) and "
                "%d institution(s)",
                len(args.pi_names),
                len(args.institutions),
            )
            sys.exit(1)
        for name, inst in zip(args.pi_names, args.institutions):
            pis.append({"pi_name": name.strip(), "institution": inst.strip()})

    if not pis:
        logger.error(
            "No PIs specified. Use --pi-file or --pi/--institution flags."
        )
        sys.exit(1)

    return pis


def main(argv=None):
    args = _parse_args(argv)
    pis = _load_pis(args)

    logger.info(
        "run_rescrape: %d PI(s), situation=%r, dry_run=%s, skip_scholar=%s",
        len(pis),
        args.situation,
        args.dry_run,
        args.skip_scholar,
    )

    config = load_config()

    summary = rescrape_and_enrich(
        pis=pis,
        situation=args.situation,
        config=config,
        dry_run=args.dry_run,
        skip_scholar=args.skip_scholar,
        force=args.force,
        tab_name=args.tab,
    )

    print("\n=== Rescrape Summary ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
