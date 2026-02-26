"""
Google Sheets client — read, write, and batch-update the Master List sheet.
"""
import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from src.config import Config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Canonical column order for the Master List tab
MASTER_LIST_SCHEMA: list[str] = [
    "institution",
    "department_program",
    "pi_name",
    "lab_research_summary",
    "lab_homepage_url",
    "lab_members_url",
    "member_name",
    "member_role",
    "recent_papers",
    "scholar_lookup_status",
    "relevance_flag",
    "relevance_reasoning",
    "email",
    "last_enriched",
    "situation_of_interest",
]

# Last column letter for batchUpdate ranges (A=1 … O=15)
_LAST_COL = chr(64 + len(MASTER_LIST_SCHEMA))  # 'O'


class SheetsClient:
    def __init__(self, config: Config) -> None:
        creds = service_account.Credentials.from_service_account_file(
            config.google_service_account_key_path, scopes=SCOPES
        )
        self._service = build("sheets", "v4", credentials=creds)
        self._sheet_id = config.google_sheet_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_sheet_metadata(self) -> list[dict]:
        result = self._service.spreadsheets().get(
            spreadsheetId=self._sheet_id
        ).execute()
        return [
            {"sheetId": s["properties"]["sheetId"], "title": s["properties"]["title"]}
            for s in result.get("sheets", [])
        ]

    def _get_sheet_id_by_name(self, tab_name: str) -> int | None:
        for s in self._get_sheet_metadata():
            if s["title"] == tab_name:
                return s["sheetId"]
        return None

    def _range(self, tab_name: str, spec: str) -> str:
        return f"'{tab_name}'!{spec}"

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def create_tab(self, tab_name: str) -> int:
        """Create a new tab and return its integer sheet ID."""
        body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        resp = self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._sheet_id, body=body
        ).execute()
        sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        logger.debug("Created tab '%s' (id=%s)", tab_name, sheet_id)
        return sheet_id

    def delete_tab(self, tab_name: str) -> None:
        """Delete a tab by name. No-op if it doesn't exist."""
        sheet_id = self._get_sheet_id_by_name(tab_name)
        if sheet_id is None:
            return
        body = {"requests": [{"deleteSheet": {"sheetId": sheet_id}}]}
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._sheet_id, body=body
        ).execute()
        logger.debug("Deleted tab '%s'", tab_name)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_all_rows(self, tab_name: str) -> list[dict]:
        """
        Return every data row as a dict keyed by the header row.
        Empty cells are returned as empty strings.
        """
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self._sheet_id,
            range=self._range(tab_name, "A:ZZ"),
        ).execute()
        values = result.get("values", [])
        if not values:
            return []
        headers = values[0]
        rows = []
        for row in values[1:]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(dict(zip(headers, padded)))
        return rows

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append_rows(self, tab_name: str, rows: list[dict]) -> None:
        """
        Append rows to a tab using MASTER_LIST_SCHEMA column order.
        If the tab is empty, a header row is written first.
        """
        if not rows:
            return

        # Check whether the tab already has a header
        existing = self._service.spreadsheets().values().get(
            spreadsheetId=self._sheet_id,
            range=self._range(tab_name, "A1:A1"),
        ).execute()
        has_header = bool(existing.get("values"))

        to_write: list[list] = []
        if not has_header:
            to_write.append(MASTER_LIST_SCHEMA)
        for row in rows:
            to_write.append([row.get(col, "") for col in MASTER_LIST_SCHEMA])

        self._service.spreadsheets().values().append(
            spreadsheetId=self._sheet_id,
            range=self._range(tab_name, "A1"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": to_write},
        ).execute()
        logger.debug("Appended %d row(s) to '%s'", len(rows), tab_name)

    def update_rows(
        self,
        tab_name: str,
        updates: list[dict],
        key_columns: list[str],
    ) -> None:
        """
        Find existing rows that match key_columns and overwrite them with
        the provided update dicts. Uses batchUpdate to minimise API calls.
        """
        if not updates:
            return

        result = self._service.spreadsheets().values().get(
            spreadsheetId=self._sheet_id,
            range=self._range(tab_name, f"A:{_LAST_COL}"),
        ).execute()
        values = result.get("values", [])
        if not values:
            logger.warning("update_rows: tab '%s' is empty", tab_name)
            return

        headers = values[0]
        data_rows = values[1:]
        n_cols = len(MASTER_LIST_SCHEMA)

        # Build a lookup: tuple of key values -> (0-based) index in data_rows
        key_indices = [headers.index(k) for k in key_columns if k in headers]
        index: dict[tuple, int] = {}
        for i, row in enumerate(data_rows):
            padded = row + [""] * (n_cols - len(row))
            key = tuple(padded[j] for j in key_indices)
            index[key] = i

        batch_data = []
        for update in updates:
            key = tuple(update.get(k, "") for k in key_columns)
            if key not in index:
                logger.warning("update_rows: no match found for key %s", key)
                continue
            row_idx = index[key]
            sheet_row = row_idx + 2  # +1 for 1-index, +1 for header row

            current = data_rows[row_idx]
            padded = current + [""] * (n_cols - len(current))
            row_dict = dict(zip(headers, padded))
            row_dict.update(update)
            new_row = [row_dict.get(col, "") for col in MASTER_LIST_SCHEMA]

            batch_data.append({
                "range": self._range(tab_name, f"A{sheet_row}:{_LAST_COL}{sheet_row}"),
                "values": [new_row],
            })

        if batch_data:
            self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"valueInputOption": "RAW", "data": batch_data},
            ).execute()
            logger.debug("Updated %d row(s) in '%s'", len(batch_data), tab_name)

    def batch_update(
        self,
        tab_name: str,
        cell_updates: list[tuple[str, Any]],
    ) -> None:
        """
        Write arbitrary cell values in a single API call.
        cell_updates is a list of (cell_ref, value) tuples, e.g. [("A1", "hello")].
        """
        if not cell_updates:
            return
        data = [
            {
                "range": self._range(tab_name, cell),
                "values": [[value]],
            }
            for cell, value in cell_updates
        ]
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self._sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        logger.debug("batch_update: wrote %d cell(s) in '%s'", len(data), tab_name)
