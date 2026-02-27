"""
Integration test for Pipeline 2, Step 5: P2 sheet writer.

Uses a "Test_P2_Write" scratch tab. Runs two rounds to verify:
  - Relevance columns always overwritten
  - Scholar columns written only when data is provided
  - Email is append-only (set on first run, preserved on second run)
  - A member not in relevance_scores still gets relevance_flag=False
"""
import pytest

from src.config import load_config
from src.pipeline2.step2_scholar_lookup import ScholarResult
from src.pipeline2.step5_sheet_writer import write_pipeline2_results
from src.sheets import SheetsClient

TAB_NAME = "Test_P2_Write"
SITUATION_1 = "CRISPR gene editing for cancer therapy"
SITUATION_2 = "Machine learning for drug discovery"

# ---------------------------------------------------------------------------
# Seed rows — Pipeline 1 data only
# ---------------------------------------------------------------------------
SEED_ROWS = [
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Develops CRISPR tools for oncogenic mutation correction.",
        "member_name": "Alice Chen",
        "member_role": "PhD Student",
        "status": "active",
    },
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Develops CRISPR tools for oncogenic mutation correction.",
        "member_name": "Bob Rivera",
        "member_role": "Postdoc",
        "status": "active",
    },
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": "Develops CRISPR tools for oncogenic mutation correction.",
        "member_name": "Carol Kim",
        "member_role": "Research Scientist",
        "status": "active",
    },
]

# ---------------------------------------------------------------------------
# Run 1 mock data
# ---------------------------------------------------------------------------
SCORES_RUN1 = [
    {
        "pi_name": "Jane Smith",
        "member_name": "Alice Chen",
        "institution": "MIT",
        "relevance_flag": True,
        "relevance_reasoning": "CRISPR base editing publications directly match the situation.",
    },
    {
        "pi_name": "Jane Smith",
        "member_name": "Bob Rivera",
        "institution": "MIT",
        "relevance_flag": False,
        "relevance_reasoning": "Networking papers have no connection to gene editing.",
    },
    # Carol Kim is intentionally absent from run-1 scores (simulates not passing coarse filter)
]

SCHOLAR_RUN1: dict[tuple[str, str], ScholarResult] = {
    ("Jane Smith", "Alice Chen"): ScholarResult(
        status="found",
        papers=["CRISPR base editing for oncogenic KRAS", "HDR in T cells for therapy"],
        profile_url="https://scholar.google.com/citations?user=fake123",
    ),
    ("Jane Smith", "Bob Rivera"): ScholarResult(
        status="not_found",
        papers=[],
    ),
}

EMAILS_RUN1: dict[tuple[str, str], str] = {
    ("Jane Smith", "Alice Chen"): "alice@mit.edu",
    ("Jane Smith", "Bob Rivera"): "",          # not found
    ("Jane Smith", "Carol Kim"): "",           # not found
}

# ---------------------------------------------------------------------------
# Run 2 mock data — reversed relevance, new situation, carol gets email
# ---------------------------------------------------------------------------
SCORES_RUN2 = [
    {
        "pi_name": "Jane Smith",
        "member_name": "Alice Chen",
        "institution": "MIT",
        "relevance_flag": False,                # CHANGED from True
        "relevance_reasoning": "Not relevant to drug discovery.",
    },
    {
        "pi_name": "Jane Smith",
        "member_name": "Bob Rivera",
        "institution": "MIT",
        "relevance_flag": True,                 # CHANGED from False
        "relevance_reasoning": "ML methods applicable to drug discovery.",
    },
    {
        "pi_name": "Jane Smith",
        "member_name": "Carol Kim",
        "institution": "MIT",
        "relevance_flag": True,
        "relevance_reasoning": "HDR work connects to target identification.",
    },
]

SCHOLAR_RUN2: dict[tuple[str, str], ScholarResult] = {}  # no new lookups

EMAILS_RUN2: dict[tuple[str, str], str] = {
    ("Jane Smith", "Alice Chen"): "alice_new@mit.edu",   # should be IGNORED (existing)
    ("Jane Smith", "Carol Kim"): "carol@mit.edu",        # should be WRITTEN (was blank)
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    return SheetsClient(load_config())


@pytest.fixture(autouse=True, scope="module")
def setup_and_teardown(client):
    """Create Test_P2_Write tab with seed rows; delete after."""
    client.delete_tab(TAB_NAME)
    client.create_tab(TAB_NAME)
    client.append_rows(TAB_NAME, SEED_ROWS)
    yield
    client.delete_tab(TAB_NAME)


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["member_name"]: r for r in rows}


# ---------------------------------------------------------------------------
# Test: Run 1
# ---------------------------------------------------------------------------

def test_run1_writes_correctly():
    config = load_config()
    summary = write_pipeline2_results(
        relevance_scores=SCORES_RUN1,
        scholar_results=SCHOLAR_RUN1,
        emails=EMAILS_RUN1,
        situation=SITUATION_1,
        config=config,
        tab_name=TAB_NAME,
    )

    assert summary["rows_scored"] == 2,             f"Expected 2 scored, got {summary}"
    assert summary["flagged_relevant"] == 1,        f"Expected 1 relevant, got {summary}"
    assert summary["scholar_lookups_performed"] == 2, f"Expected 2 scholar, got {summary}"
    assert summary["emails_found"] == 1,            f"Expected 1 email, got {summary}"

    rows = SheetsClient(config).read_all_rows(TAB_NAME)
    by = _by_name(rows)

    # Alice — scored True, scholar found, email written
    assert by["Alice Chen"]["relevance_flag"] in (True, "TRUE", "true")
    assert by["Alice Chen"]["scholar_lookup_status"] == "found"
    assert "CRISPR" in by["Alice Chen"]["recent_papers"]
    assert by["Alice Chen"]["email"] == "alice@mit.edu"
    assert by["Alice Chen"]["last_enriched"]
    assert by["Alice Chen"]["situation_of_interest"] == SITUATION_1

    # Bob — scored False, scholar not_found, no email
    assert by["Bob Rivera"]["relevance_flag"] in (False, "FALSE", "false")
    assert by["Bob Rivera"]["scholar_lookup_status"] == "not_found"
    assert by["Bob Rivera"]["email"] == ""
    assert by["Bob Rivera"]["situation_of_interest"] == SITUATION_1

    # Carol — NOT in run-1 scores → relevance_flag=False, no scholar, no email
    assert by["Carol Kim"]["relevance_flag"] in (False, "FALSE", "false")
    assert by["Carol Kim"]["email"] == ""
    assert by["Carol Kim"]["scholar_lookup_status"] == ""   # no scholar data written

    print(f"\n{'='*65}")
    print(f"Run 1 — situation: {SITUATION_1}")
    print(f"{'='*65}")
    for name, r in by.items():
        print(
            f"  {name:<20} | flag={r.get('relevance_flag','?')!s:<5} "
            f"| scholar={r.get('scholar_lookup_status','?'):<10} "
            f"| email={r.get('email','?') or '(none)':<20} "
            f"| enriched={bool(r.get('last_enriched'))}"
        )
    print(f"\n  Summary: {summary}")


# ---------------------------------------------------------------------------
# Test: Run 2 — overwrite relevance, preserve email, write new email
# ---------------------------------------------------------------------------

def test_run2_overwrite_and_preserve():
    config = load_config()
    summary = write_pipeline2_results(
        relevance_scores=SCORES_RUN2,
        scholar_results=SCHOLAR_RUN2,
        emails=EMAILS_RUN2,
        situation=SITUATION_2,
        config=config,
        tab_name=TAB_NAME,
    )

    assert summary["rows_scored"] == 3,      f"Expected 3 scored, got {summary}"
    assert summary["flagged_relevant"] == 2, f"Expected 2 relevant, got {summary}"
    assert summary["scholar_lookups_performed"] == 0
    assert summary["emails_found"] == 1,     f"Expected 1 new email (Carol), got {summary}"

    rows = SheetsClient(config).read_all_rows(TAB_NAME)
    by = _by_name(rows)

    # Alice — relevance overwritten to False; email PRESERVED (not cleared)
    assert by["Alice Chen"]["relevance_flag"] in (False, "FALSE", "false"), (
        "Alice's relevance should have flipped to False"
    )
    assert by["Alice Chen"]["email"] == "alice@mit.edu", (
        "Alice's email must be preserved, not overwritten by alice_new@"
    )
    assert by["Alice Chen"]["situation_of_interest"] == SITUATION_2

    # Bob — relevance overwritten to True
    assert by["Bob Rivera"]["relevance_flag"] in (True, "TRUE", "true"), (
        "Bob's relevance should have flipped to True"
    )

    # Carol — relevance written for first time (True); email written for first time
    assert by["Carol Kim"]["relevance_flag"] in (True, "TRUE", "true")
    assert by["Carol Kim"]["email"] == "carol@mit.edu", (
        "Carol's email should be written on run 2"
    )

    # Scholar columns from run 1 should still be present (not cleared)
    assert by["Alice Chen"]["scholar_lookup_status"] == "found", (
        "Run 2 should not clear Alice's scholar status"
    )

    print(f"\n{'='*65}")
    print(f"Run 2 — situation: {SITUATION_2}")
    print(f"{'='*65}")
    for name, r in by.items():
        print(
            f"  {name:<20} | flag={r.get('relevance_flag','?')!s:<5} "
            f"| scholar={r.get('scholar_lookup_status','?'):<10} "
            f"| email={r.get('email','?') or '(none)':<20} "
            f"| situation={r.get('situation_of_interest','?')}"
        )
    print(f"\n  Summary: {summary}")
