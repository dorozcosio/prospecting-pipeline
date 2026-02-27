"""
Pipeline 1, Step 6: Sync member rows to the Master List Google Sheet tab.

Strategy:
  - Read all existing rows from the tab (keyed by composite key).
  - For each incoming row:
      * NEW  → append with status="active"
      * CHANGED (P1 fields only) → update changed fields
      * UNCHANGED → skip
  - Rows previously in the sheet but NOT in this run → mark status="inactive"
    (only if they were status="active"; P2 fields are never cleared)

Composite key: (institution, pi_name, member_name)  — all lowercased/stripped.

P1-owned columns (safe to overwrite):
    institution, department_program, pi_name, lab_research_summary,
    lab_homepage_url, lab_members_url, member_name, member_role, status

P2-owned columns (never touched by this step):
    recent_papers, scholar_lookup_status, relevance_flag,
    relevance_reasoning, email, last_enriched, situation_of_interest
"""
import logging

from src.config import Config
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

# Fields this step is allowed to write / overwrite
_P1_FIELDS: set[str] = {
    "institution",
    "department_program",
    "pi_name",
    "lab_research_summary",
    "lab_homepage_url",
    "lab_members_url",
    "member_name",
    "member_role",
    "status",
}

_KEY_COLS = ("institution", "pi_name", "member_name")


def _composite_key(row: dict) -> tuple[str, str, str]:
    return (
        row.get("institution", "").strip().lower(),
        row.get("pi_name", "").strip().lower(),
        row.get("member_name", "").strip().lower(),
    )


def write_pipeline1_results(
    members: list[dict],
    config: Config,
    tab_name: str = "Master List",
) -> dict:
    """
    Sync a flat list of member dicts (from Step 5) to a Google Sheet tab.

    Args:
        members:  Output of extract_members() — one dict per person.
        config:   Loaded Config.
        tab_name: Target tab name (default "Master List").

    Returns:
        {"added": int, "updated": int, "deactivated": int, "unchanged": int}
    """
    client = SheetsClient(config)

    # ------------------------------------------------------------------
    # 1. Ensure the tab exists
    # ------------------------------------------------------------------
    if client._get_sheet_id_by_name(tab_name) is None:
        client.create_tab(tab_name)
        logger.info("Created tab '%s'", tab_name)

    # ------------------------------------------------------------------
    # 2. Read existing rows
    # ------------------------------------------------------------------
    existing_rows = client.read_all_rows(tab_name)
    existing_index: dict[tuple, dict] = {}   # key → row dict
    existing_sheet_rows: dict[tuple, int] = {}  # key → 1-based sheet row
    for i, row in enumerate(existing_rows):
        k = _composite_key(row)
        existing_index[k] = row
        existing_sheet_rows[k] = i + 2  # +1 for 1-index, +1 for header

    # ------------------------------------------------------------------
    # 3. Classify incoming rows
    # ------------------------------------------------------------------
    incoming_keys: set[tuple] = set()
    to_append: list[dict] = []
    to_update: list[dict] = []
    unchanged_count = 0

    for member in members:
        row = {**member, "status": member.get("status", "active")}
        key = _composite_key(row)
        incoming_keys.add(key)

        if key not in existing_index:
            row["status"] = "active"
            to_append.append(row)
        else:
            existing = existing_index[key]
            # Compare only P1-owned fields (excluding status, which we manage)
            changed = any(
                row.get(f, "") != existing.get(f, "")
                for f in _P1_FIELDS - {"status"}
            )
            if changed:
                update = {col: existing.get(col, "") for col in existing}
                for f in _P1_FIELDS - {"status"}:
                    update[f] = row.get(f, "")
                update["status"] = "active"
                to_update.append(update)
            else:
                unchanged_count += 1

    # ------------------------------------------------------------------
    # 4. Deactivate rows no longer in this run
    # ------------------------------------------------------------------
    to_deactivate: list[dict] = []
    for key, existing in existing_index.items():
        if key not in incoming_keys and existing.get("status", "") == "active":
            deactivated = {col: existing.get(col, "") for col in existing}
            deactivated["status"] = "inactive"
            to_deactivate.append(deactivated)

    # ------------------------------------------------------------------
    # 5. Write to Sheets
    # ------------------------------------------------------------------
    if to_append:
        client.append_rows(tab_name, to_append)
        logger.info("Appended %d new row(s) to '%s'", len(to_append), tab_name)

    if to_update:
        client.update_rows(tab_name, to_update, list(_KEY_COLS))
        logger.info("Updated %d row(s) in '%s'", len(to_update), tab_name)

    if to_deactivate:
        client.update_rows(tab_name, to_deactivate, list(_KEY_COLS))
        logger.info("Deactivated %d row(s) in '%s'", len(to_deactivate), tab_name)

    summary = {
        "added": len(to_append),
        "updated": len(to_update),
        "deactivated": len(to_deactivate),
        "unchanged": unchanged_count,
    }
    logger.info(
        "write_pipeline1_results '%s': +%d new, ~%d updated, -%d deactivated, =%d unchanged",
        tab_name,
        summary["added"],
        summary["updated"],
        summary["deactivated"],
        summary["unchanged"],
    )
    return summary
