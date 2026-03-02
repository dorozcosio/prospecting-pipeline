"""
Integration tests for scoped deactivation in Pipeline 1, Step 6.

Verifies that when write_pipeline1_results is called for a subset of
institutions, only rows belonging to those institutions are eligible for
deactivation.  Rows from institutions NOT in scope must be left completely
untouched.

Uses a temporary "Test_ScopedDeact" sheet tab; cleaned up before and after
the module runs.
"""
import pytest

from src.config import load_config
from src.pipeline1.step6_sheet_writer import init_run_cache, write_pipeline1_results
from src.sheets import SheetsClient

TAB_NAME = "Test_ScopedDeact"

# ---------------------------------------------------------------------------
# Test data — two independent institutions
# ---------------------------------------------------------------------------

_BASE_A = {
    "institution": "TestUnivA",
    "department_program": "CS",
    "pi_name": "Prof Alpha",
    "lab_research_summary": "Robotics research.",
    "lab_homepage_url": "",
    "lab_members_url": "",
}

_BASE_B = {
    "institution": "TestUnivB",
    "department_program": "Biology",
    "pi_name": "Prof Beta",
    "lab_research_summary": "Genomics research.",
    "lab_homepage_url": "",
    "lab_members_url": "",
}

# Initial state: 2 members per institution
_INITIAL_MEMBERS = [
    {**_BASE_A, "member_name": "Alice", "member_role": "PhD Student"},
    {**_BASE_A, "member_name": "Bob",   "member_role": "Postdoc"},      # will be removed
    {**_BASE_B, "member_name": "Charlie", "member_role": "PhD Student"},
    {**_BASE_B, "member_name": "Diana",   "member_role": "Postdoc"},
]

# TestUnivA rescrape: Bob is gone
_UNIVA_MEMBERS = [
    {**_BASE_A, "member_name": "Alice", "member_role": "PhD Student"},
]

# TestUnivB rescrape: both still present
_UNIVB_MEMBERS = [
    {**_BASE_B, "member_name": "Charlie", "member_role": "PhD Student"},
    {**_BASE_B, "member_name": "Diana",   "member_role": "Postdoc"},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def client(config):
    return SheetsClient(config)


@pytest.fixture(autouse=True, scope="module")
def clean_tab(client):
    """Delete Test_ScopedDeact before and after the module runs."""
    client.delete_tab(TAB_NAME)
    yield
    client.delete_tab(TAB_NAME)


# ---------------------------------------------------------------------------
# Tests (must run in order)
# ---------------------------------------------------------------------------

def test_initial_write_both_institutions(config, client):
    """Seed the sheet with 4 rows across two institutions."""
    init_run_cache()
    result = write_pipeline1_results(
        _INITIAL_MEMBERS,
        config,
        institutions_in_run=["TestUnivA", "TestUnivB"],
        tab_name=TAB_NAME,
        deactivate_missing=False,
    )

    assert result["added"] == 4
    assert result["deactivated"] == 0

    rows = client.read_all_rows(TAB_NAME)
    assert len(rows) == 4
    for row in rows:
        assert row["status"] == "active"


def test_univa_run_deactivates_only_univa_member(config, client):
    """
    Rescrape TestUnivA with Bob removed.
    Bob (TestUnivA) must be deactivated; Charlie and Diana (TestUnivB) untouched.
    """
    init_run_cache()
    result = write_pipeline1_results(
        _UNIVA_MEMBERS,
        config,
        institutions_in_run=["TestUnivA"],
        tab_name=TAB_NAME,
        deactivate_missing=True,
    )

    assert result["deactivated"] == 1, f"Expected Bob deactivated, got {result}"

    rows = client.read_all_rows(TAB_NAME)
    by_name = {r["member_name"]: r for r in rows}

    # TestUnivA: Alice active, Bob inactive
    assert by_name["Alice"]["status"] == "active"
    assert by_name["Bob"]["status"] == "inactive"

    # TestUnivB: completely untouched
    assert by_name["Charlie"]["status"] == "active", "Charlie should still be active"
    assert by_name["Diana"]["status"] == "active",   "Diana should still be active"
    assert by_name["Charlie"]["institution"] == "TestUnivB"
    assert by_name["Diana"]["institution"] == "TestUnivB"


def test_univb_run_leaves_univa_untouched(config, client):
    """
    Rescrape TestUnivB (all members still present).
    TestUnivA rows (Alice active, Bob inactive) must not change.
    """
    init_run_cache()
    result = write_pipeline1_results(
        _UNIVB_MEMBERS,
        config,
        institutions_in_run=["TestUnivB"],
        tab_name=TAB_NAME,
        deactivate_missing=True,
    )

    # Nothing deactivated — both TestUnivB members are still present
    assert result["deactivated"] == 0, f"Expected 0 deactivations, got {result}"

    rows = client.read_all_rows(TAB_NAME)
    by_name = {r["member_name"]: r for r in rows}

    # TestUnivA rows must be exactly as left after the previous run
    assert by_name["Alice"]["status"] == "active",   "Alice should still be active"
    assert by_name["Bob"]["status"] == "inactive",   "Bob should still be inactive"

    # TestUnivB rows are active
    assert by_name["Charlie"]["status"] == "active"
    assert by_name["Diana"]["status"] == "active"
