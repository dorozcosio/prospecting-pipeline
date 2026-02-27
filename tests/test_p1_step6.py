"""
Integration test for Pipeline 1, Step 6: Sheet writer.

Uses a temporary "Test_P1" tab to avoid touching the real Master List.
Runs two rounds to verify add / update / unchanged / deactivate counts.
"""
import pytest

from src.config import load_config
from src.pipeline1.step6_sheet_writer import write_pipeline1_results
from src.sheets import SheetsClient

TAB_NAME = "Test_P1"

# ---------------------------------------------------------------------------
# Mock member data — two PIs, three members
# ---------------------------------------------------------------------------
RUN1_MEMBERS = [
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Jane Smith",
        "member_role": "PI",
    },
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Alex Johnson",
        "member_role": "PhD Student",
    },
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Maria Garcia",
        "member_role": "Postdoc",
    },
]

# Run 2: Alex promoted to Postdoc (update), Maria leaves (deactivate),
#         new member Sam Lee added (add), Jane unchanged.
RUN2_MEMBERS = [
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Jane Smith",
        "member_role": "PI",
    },
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Alex Johnson",
        "member_role": "Postdoc",          # was PhD Student → update
    },
    {
        "institution": "MIT",
        "department_program": "EECS",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Studies AI in healthcare.",
        "lab_homepage_url": "https://example.com/smith-lab",
        "lab_members_url": "https://example.com/smith-lab/people",
        "member_name": "Sam Lee",          # new → add
        "member_role": "Research Scientist",
    },
    # Maria Garcia absent → deactivated
]


@pytest.fixture(scope="module")
def client():
    config = load_config()
    return SheetsClient(config)


@pytest.fixture(autouse=True, scope="module")
def clean_tab(client):
    """Delete Test_P1 before and after the module runs."""
    client.delete_tab(TAB_NAME)
    yield
    client.delete_tab(TAB_NAME)


def test_run1_all_new():
    config = load_config()
    result = write_pipeline1_results(RUN1_MEMBERS, config, tab_name=TAB_NAME)

    assert result["added"] == 3, f"Expected 3 new rows, got {result}"
    assert result["updated"] == 0
    assert result["deactivated"] == 0
    assert result["unchanged"] == 0

    # Verify sheet contents
    client = SheetsClient(config)
    rows = client.read_all_rows(TAB_NAME)
    assert len(rows) == 3, f"Expected 3 rows in sheet, got {len(rows)}"
    for row in rows:
        assert row["status"] == "active", f"Expected active, got {row['status']}"

    print(f"\n{'='*60}")
    print("Run 1 — all new:")
    print(f"{'='*60}")
    _print_rows(rows)
    print(f"\nResult: {result}")


def test_run2_update_deactivate_add():
    config = load_config()
    result = write_pipeline1_results(RUN2_MEMBERS, config, tab_name=TAB_NAME)

    assert result["added"] == 1,       f"Expected 1 added, got {result}"
    assert result["updated"] == 1,     f"Expected 1 updated, got {result}"
    assert result["deactivated"] == 1, f"Expected 1 deactivated, got {result}"
    assert result["unchanged"] == 1,   f"Expected 1 unchanged, got {result}"

    # Verify sheet state
    client = SheetsClient(config)
    rows = client.read_all_rows(TAB_NAME)
    assert len(rows) == 4, f"Expected 4 rows total, got {len(rows)}"

    by_name = {r["member_name"]: r for r in rows}

    assert by_name["Jane Smith"]["status"] == "active"
    assert by_name["Alex Johnson"]["status"] == "active"
    assert by_name["Alex Johnson"]["member_role"] == "Postdoc"
    assert by_name["Maria Garcia"]["status"] == "inactive"
    assert by_name["Sam Lee"]["status"] == "active"

    print(f"\n{'='*60}")
    print("Run 2 — update / deactivate / add:")
    print(f"{'='*60}")
    _print_rows(rows)
    print(f"\nResult: {result}")


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(
            f"  [{row.get('status','?'):8}] "
            f"{row.get('member_name',''):<30} | "
            f"{row.get('member_role',''):<25} | "
            f"{row.get('pi_name','')}"
        )
