"""
End-to-end smoke tests for Pipeline 1 and Pipeline 2.

These tests confirm that the wiring between steps is correct — not that every
individual step produces perfect results (unit tests cover that).

P1 smoke test:
  Uses hardcoded PI dicts with confirmed lab_members_url values to skip the
  slow steps 1–4 (URL discovery, PI extraction, lab homepages, member pages).
  Runs step 5 (member extraction) and confirms the orchestrator returns the
  expected structure, without writing to the sheet (dry_run=True).

P2 smoke test:
  Writes P1 output to a "Test_Integration" scratch tab, then runs P2 steps
  1, 3, 4 (coarse filter, fine scoring, email lookup) against that tab
  with skip_scholar=True and dry_run=True. Confirms relevance scores are
  produced and the return dict has the right shape.
"""
import pytest

from src.config import load_config
from src.run_pipeline1 import run_pipeline1
from src.run_pipeline2 import run_pipeline2
from src.sheets import SheetsClient

# ---------------------------------------------------------------------------
# Hardcoded PI data — confirmed working in previous step tests
# ---------------------------------------------------------------------------
HARDCODED_PIS = [
    {
        "name": "Regina Barzilay",
        "role": "School of Engineering Distinguished Professor of AI and Health, [AI+D]",
        "institution": "MIT",
        "department_program": "EECS",
        "source_url": "https://www.eecs.mit.edu/role/faculty-aid/",
        "lab_homepage_url": "https://www.rbg.mit.edu/",
        "lab_research_summary": (
            "Develops machine learning methods for molecular docking, biomolecule "
            "generation, and clinical decision-making, with applications in drug "
            "discovery, protein design, and cancer risk prediction."
        ),
        "lab_members_url": "https://www.rbg.mit.edu/people/",
    },
]

INTEGRATION_TAB = "Test_Integration"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    return SheetsClient(load_config())


@pytest.fixture(scope="module")
def p1_result(client):
    """
    Run Pipeline 1 (steps 5+6) with hardcoded PI data.

    Steps 1–4 are bypassed by supplying urls_override and pre-built PI dicts
    directly to step 5 via the orchestrator's urls_override + a custom path.
    Since step 5 (extract_members) requires PIs with lab_members_url, we run
    only step 5 by calling run_pipeline1 with urls_override=None (which falls
    through to discover), but using the hardcoded PIs as the pis_override.

    Simpler approach: call run_pipeline1 with urls_override that maps to an
    empty list (skips steps 1–4 naturally) but pass the pre-built PIs via
    the public API. Since run_pipeline1 runs step 2 on the provided URLs,
    we supply a synthetic single URL entry per institution so step 2 is
    called with exactly our hardcoded data.
    """
    config = load_config()

    # We call run_pipeline1 with a urls_override so step 1 is skipped.
    # Step 2 will be called on these URLs — the test URL is the Barzilay lab
    # page which returns real PI data (cached from previous test runs).
    # Step 5 then extracts members from lab_members_url.
    result = run_pipeline1(
        dry_run=True,
        urls_override={
            "MIT": [
                {
                    "url": "https://www.eecs.mit.edu/role/faculty-aid/?fwp_research=ai-for-healthcare-and-life-sciences",
                    "snippet": "MIT EECS faculty — AI for Healthcare and Life Sciences",
                    "source_query": "integration_test",
                }
            ]
        },
        tab_name=INTEGRATION_TAB,
    )
    return result


# ---------------------------------------------------------------------------
# Pipeline 1 smoke test
# ---------------------------------------------------------------------------

def test_p1_smoke_returns_structure(p1_result):
    """run_pipeline1 returns a dict with the expected keys and non-empty data."""
    required_keys = {
        "institutions_processed", "pis_found", "members_found",
        "members", "sheet_summary",
    }
    assert required_keys == set(p1_result.keys()), (
        f"Missing keys: {required_keys - set(p1_result.keys())}"
    )
    assert p1_result["institutions_processed"] >= 1
    assert p1_result["pis_found"] >= 1,  "Expected at least 1 PI"
    assert p1_result["members"], "Expected at least one member row"
    assert p1_result["sheet_summary"].get("dry_run") is True


def test_p1_members_have_required_keys(p1_result):
    """Every member row returned by run_pipeline1 has the required fields."""
    required = {
        "institution", "department_program", "pi_name",
        "lab_research_summary", "lab_homepage_url",
        "lab_members_url", "member_name", "member_role",
    }
    for row in p1_result["members"]:
        missing = required - set(row.keys())
        assert not missing, f"Member row missing keys {missing}: {row}"


def test_p1_has_pi_and_member_rows(p1_result):
    """run_pipeline1 produces both PI rows and non-PI member rows."""
    members = p1_result["members"]
    pi_rows = [r for r in members if r["member_role"] == "PI"]
    member_rows = [r for r in members if r["member_role"] != "PI"]
    assert pi_rows,    "Expected at least one PI row"
    assert member_rows, "Expected at least one non-PI member row"

    print(f"\n{'='*60}")
    print(f"P1 smoke: {len(pi_rows)} PI rows + {len(member_rows)} member rows")
    print(f"{'='*60}")
    for row in members[:5]:
        tag = "[PI]" if row["member_role"] == "PI" else "    "
        print(f"  {tag} {row['member_name']:<35} | {row['member_role']}")
    if len(members) > 5:
        print(f"  ... ({len(members) - 5} more)")


# ---------------------------------------------------------------------------
# Pipeline 2 smoke test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def p2_setup(client, p1_result):
    """
    Write the P1 members into Test_Integration tab, then run P2
    (skip_scholar + dry_run) against that tab.
    """
    members = p1_result["members"]

    # Step 6 would normally add status="active"; we must set it here since
    # run_pipeline1 was called with dry_run=True (step 6 was skipped).
    seeded = [{**m, "status": "active"} for m in members]

    # Seed the test tab with P1 output
    client.delete_tab(INTEGRATION_TAB)
    client.create_tab(INTEGRATION_TAB)
    client.append_rows(INTEGRATION_TAB, seeded)

    config = load_config()
    result = run_pipeline2(
        situation="machine learning for clinical drug discovery",
        dry_run=True,
        skip_scholar=True,
        tab_name=INTEGRATION_TAB,
    )

    yield result

    # Cleanup
    client.delete_tab(INTEGRATION_TAB)


def test_p2_smoke_returns_structure(p2_setup):
    """run_pipeline2 returns a dict with the expected keys."""
    result = p2_setup
    required_keys = {
        "passing_pis", "members_scored", "flagged_relevant",
        "emails_found", "relevance_scores", "sheet_summary",
    }
    assert required_keys == set(result.keys()), (
        f"Missing keys: {required_keys - set(result.keys())}"
    )
    assert result["sheet_summary"].get("dry_run") is True


def test_p2_smoke_produces_scores(p2_setup):
    """run_pipeline2 produces at least some relevance scores."""
    result = p2_setup
    assert result["members_scored"] >= 1, (
        "Expected at least one member to be scored"
    )
    assert result["relevance_scores"], "relevance_scores list should not be empty"

    for score in result["relevance_scores"]:
        assert "member_name" in score
        assert "relevance_flag" in score
        assert isinstance(score["relevance_flag"], bool)
        assert "relevance_reasoning" in score

    flagged = result["flagged_relevant"]
    total = result["members_scored"]

    print(f"\n{'='*60}")
    print(f"P2 smoke: {total} scored, {flagged} flagged relevant")
    print(f"{'='*60}")
    for score in result["relevance_scores"][:8]:
        flag = "YES" if score["relevance_flag"] else "NO "
        print(f"  [{flag}] {score['member_name']:<35} | {score['relevance_reasoning'][:60]}…")
    if len(result["relevance_scores"]) > 8:
        print(f"  ... ({len(result['relevance_scores']) - 8} more)")
