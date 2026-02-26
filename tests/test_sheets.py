"""
Integration test: Google Sheets connectivity.

Creates a 'Test' tab, writes a row, reads it back, asserts it matches,
then deletes the tab. Confirms the full auth chain end-to-end.
"""
import pytest

from src.config import load_config
from src.sheets import MASTER_LIST_SCHEMA, SheetsClient

TEST_TAB = "Test"


@pytest.fixture(scope="module")
def client() -> SheetsClient:
    return SheetsClient(load_config())


@pytest.fixture(autouse=True)
def clean_test_tab(client: SheetsClient):
    """Remove the Test tab before and after each test so we start fresh."""
    client.delete_tab(TEST_TAB)
    yield
    client.delete_tab(TEST_TAB)


def test_write_and_read_row(client: SheetsClient):
    client.create_tab(TEST_TAB)

    test_row = {col: f"test_{col}" for col in MASTER_LIST_SCHEMA}
    client.append_rows(TEST_TAB, [test_row])

    rows = client.read_all_rows(TEST_TAB)
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    assert rows[0] == test_row, f"Row mismatch:\n  wrote:    {test_row}\n  read back:{rows[0]}"
    print(f"\n✓ Wrote and read back 1 row with {len(MASTER_LIST_SCHEMA)} columns")
