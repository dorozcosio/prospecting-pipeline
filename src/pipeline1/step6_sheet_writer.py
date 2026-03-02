"""
Pipeline 1, Step 6: Sync member rows to the Master List Google Sheet tab.

Strategy:
  - Read all existing rows from the tab (keyed by composite key).
    The read result is cached in memory; subsequent calls within the same
    process reuse the cache so the sheet is only read once per run.
  - For each incoming row:
      * NEW  → append with status="active", institution_last_scraped=now
      * PREVIOUSLY INACTIVE but present again → reactivate to "active",
        update institution_last_scraped
      * CHANGED (P1 fields only) → update changed fields + institution_last_scraped
      * UNCHANGED → skip
  - Deactivation (only when deactivate_missing=True):
      Rows that were previously "active" but are absent from the incoming
      batch are marked status="inactive" — scoped to `institutions_in_run`
      (or inferred from incoming data when not provided) so cross-institution
      rows are never touched.

Composite key: (institution, pi_name, member_name)  — all lowercased/stripped.

P1-owned columns (safe to overwrite):
    institution, department_program, pi_name, lab_research_summary,
    lab_homepage_url, lab_members_url, member_name, member_role, status,
    institution_last_scraped

P2-owned columns (never touched by this step):
    recent_papers, scholar_lookup_status, relevance_flag,
    relevance_reasoning, email, last_enriched, situation_of_interest
"""
import logging
from datetime import datetime, timezone

from src.config import Config
from src.sheets import SheetsClient

logger = logging.getLogger(__name__)

# Fields this step is allowed to compare/overwrite (status and
# institution_last_scraped are handled separately)
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

# Module-level sheet-read cache.  Populated on the first call to
# write_pipeline1_results() within a process; cleared by init_run_cache().
_cache: dict[tuple, dict] | None = None


def init_run_cache() -> None:
    """Reset the in-memory sheet cache.  Call this at the start of each run."""
    global _cache
    _cache = None
    logger.debug("step6 sheet cache cleared")


def _composite_key(row: dict) -> tuple[str, str, str]:
    return (
        row.get("institution", "").strip().lower(),
        row.get("pi_name", "").strip().lower(),
        row.get("member_name", "").strip().lower(),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_pipeline1_results(
    members: list[dict],
    config: Config,
    institutions_in_run: list[str] | None = None,
    tab_name: str = "Master List",
    deactivate_missing: bool = True,
) -> dict:
    """
    Sync a flat list of member dicts (from Step 5) to a Google Sheet tab.

    Args:
        members:             Output of extract_members() — one dict per person.
        config:              Loaded Config.
        institutions_in_run: Explicit list of institution names processed in
                             this run.  Used to scope deactivation so
                             institutions not in this run are never touched.
                             If omitted, the scope is inferred from the
                             incoming `members` data (backward-compatible).
        tab_name:            Target tab name (default "Master List").
        deactivate_missing:  When True (default), rows previously in the sheet
                             for scoped institutions that are absent from this
                             batch are marked status="inactive".
                             Pass False for incremental group writes mid-run.

    Returns:
        {"added": int, "updated": int, "reactivated": int,
         "deactivated": int, "unchanged": int}
    """
    global _cache

    now = _now_iso()
    client = SheetsClient(config)

    # ------------------------------------------------------------------
    # 1. Ensure the tab exists
    # ------------------------------------------------------------------
    if client._get_sheet_id_by_name(tab_name) is None:
        client.create_tab(tab_name)
        logger.info("Created tab '%s'", tab_name)

    # ------------------------------------------------------------------
    # 2. Read existing rows (use cache if available)
    # ------------------------------------------------------------------
    if _cache is None:
        existing_rows = client.read_all_rows(tab_name)
        _cache = {}
        for row in existing_rows:
            k = _composite_key(row)
            _cache[k] = row
        logger.debug("Sheet cache populated: %d existing rows", len(_cache))

    existing_index = _cache  # alias for clarity

    # ------------------------------------------------------------------
    # 3. Determine which institutions are in scope for deactivation
    # ------------------------------------------------------------------
    if institutions_in_run is not None:
        # Explicit scope: only deactivate rows from institutions we scraped
        run_institutions = {inst.strip().lower() for inst in institutions_in_run}
    else:
        # Backward-compatible fallback: infer scope from incoming data
        run_institutions = {m.get("institution", "").strip().lower() for m in members}

    # ------------------------------------------------------------------
    # 4. Classify incoming rows
    # ------------------------------------------------------------------
    incoming_keys: set[tuple] = set()
    to_append: list[dict] = []
    to_update: list[dict] = []
    reactivated_count = 0
    unchanged_count = 0

    for member in members:
        row = {**member, "status": member.get("status", "active")}
        key = _composite_key(row)
        incoming_keys.add(key)

        if key not in existing_index:
            row["status"] = "active"
            row["institution_last_scraped"] = now
            to_append.append(row)
        else:
            existing = existing_index[key]
            changed = any(
                row.get(f, "") != existing.get(f, "")
                for f in _P1_FIELDS - {"status"}
            )
            was_inactive = existing.get("status", "active") != "active"

            if changed or was_inactive:
                update = {col: existing.get(col, "") for col in existing}
                for f in _P1_FIELDS - {"status"}:
                    update[f] = row.get(f, "")
                update["status"] = "active"
                update["institution_last_scraped"] = now
                if was_inactive and not changed:
                    reactivated_count += 1
                to_update.append(update)
            else:
                unchanged_count += 1

    # ------------------------------------------------------------------
    # 5. Deactivate rows no longer in this run (scoped to run_institutions)
    # ------------------------------------------------------------------
    to_deactivate: list[dict] = []
    if deactivate_missing:
        out_of_scope_count = sum(
            1 for row in existing_index.values()
            if row.get("institution", "").strip().lower() not in run_institutions
        )
        logger.info(
            "Deactivation scoped to %d institution(s): %s. "
            "%d row(s) from other institutions left unchanged.",
            len(run_institutions),
            sorted(run_institutions),
            out_of_scope_count,
        )
        for key, existing in existing_index.items():
            inst = existing.get("institution", "").strip().lower()
            if (
                inst in run_institutions
                and key not in incoming_keys
                and existing.get("status", "") == "active"
            ):
                deactivated = {col: existing.get(col, "") for col in existing}
                deactivated["status"] = "inactive"
                to_deactivate.append(deactivated)

    # ------------------------------------------------------------------
    # 6. Write to Sheets
    # ------------------------------------------------------------------
    if to_append:
        client.append_rows(tab_name, to_append)
        logger.info("Appended %d new row(s) to '%s'", len(to_append), tab_name)
        for row in to_append:
            _cache[_composite_key(row)] = row

    if to_update:
        client.update_rows(tab_name, to_update, list(_KEY_COLS))
        logger.info("Updated %d row(s) in '%s'", len(to_update), tab_name)
        for row in to_update:
            _cache[_composite_key(row)] = row

    if to_deactivate:
        client.update_rows(tab_name, to_deactivate, list(_KEY_COLS))
        logger.info("Deactivated %d row(s) in '%s'", len(to_deactivate), tab_name)
        for row in to_deactivate:
            _cache[_composite_key(row)] = row

    summary = {
        "added": len(to_append),
        "updated": len(to_update),
        "reactivated": reactivated_count,
        "deactivated": len(to_deactivate),
        "unchanged": unchanged_count,
    }
    logger.info(
        "write_pipeline1_results '%s': +%d new, ~%d updated (%d reactivated), "
        "-%d deactivated, =%d unchanged",
        tab_name,
        summary["added"],
        summary["updated"],
        summary["reactivated"],
        summary["deactivated"],
        summary["unchanged"],
    )
    return summary
