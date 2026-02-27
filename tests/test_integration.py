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

Deactivation scope test:
  Verifies that write_pipeline1_results only deactivates rows from institutions
  explicitly in scope (institutions_in_run) and never touches rows from other
  institutions. Also verifies that previously inactive rows are reactivated
  when they reappear in a future run.

P2 full-scoring test:
  Verifies that after Pipeline 2 runs, ALL active rows (across all institutions)
  have situation_of_interest, relevance_flag, and relevance_reasoning populated.
  Rows under PIs that didn't pass the coarse filter should have
  relevance_reasoning = "PI lab not relevant to current situation".
"""
import pytest

from src.config import load_config
from src.pipeline1.step6_sheet_writer import init_run_cache, write_pipeline1_results
from src.pipeline2.step1_coarse_filter import coarse_filter_pis
from src.pipeline2.step5_sheet_writer import write_pipeline2_results
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
DEACTIVATION_TAB = "Test_Deactivation"
P2_SCORING_TAB = "Test_P2_Scoring"

# ---------------------------------------------------------------------------
# Test data for deactivation scope test
# ---------------------------------------------------------------------------

_INST_A = "TestInst_A"
_INST_B = "TestInst_B"

_MEMBERS_A_FULL = [
    {"institution": _INST_A, "pi_name": "PI Alpha", "member_name": "Alice",
     "member_role": "PhD Student", "department_program": "CS",
     "lab_research_summary": "ML for drug discovery",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_A, "pi_name": "PI Alpha", "member_name": "Bob",
     "member_role": "Postdoc", "department_program": "CS",
     "lab_research_summary": "ML for drug discovery",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_A, "pi_name": "PI Alpha", "member_name": "Carol",
     "member_role": "PhD Student", "department_program": "CS",
     "lab_research_summary": "ML for drug discovery",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
]

_MEMBERS_B_FULL = [
    {"institution": _INST_B, "pi_name": "PI Beta", "member_name": "Dave",
     "member_role": "PhD Student", "department_program": "Biology",
     "lab_research_summary": "Protein folding",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_B, "pi_name": "PI Beta", "member_name": "Eve",
     "member_role": "Postdoc", "department_program": "Biology",
     "lab_research_summary": "Protein folding",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_B, "pi_name": "PI Beta", "member_name": "Frank",
     "member_role": "Research Scientist", "department_program": "Biology",
     "lab_research_summary": "Protein folding",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
]

# A partial subset of TestInst_A (Carol is missing — should be deactivated)
_MEMBERS_A_PARTIAL = [m for m in _MEMBERS_A_FULL if m["member_name"] != "Carol"]


# ---------------------------------------------------------------------------
# Test data for P2 full-scoring test
# ---------------------------------------------------------------------------

_P2_MEMBERS = [
    # Relevant PI (should pass coarse filter for CRISPR situation)
    {"institution": _INST_A, "pi_name": "PI CRISPR", "member_name": "PI CRISPR",
     "member_role": "PI", "department_program": "Bioengineering",
     "lab_research_summary": "CRISPR-Cas9 gene editing for cancer therapy and genetic disease",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_A, "pi_name": "PI CRISPR", "member_name": "Grad Student A",
     "member_role": "PhD Student", "department_program": "Bioengineering",
     "lab_research_summary": "CRISPR-Cas9 gene editing for cancer therapy and genetic disease",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    # Clearly irrelevant PI (should NOT pass coarse filter for CRISPR situation)
    {"institution": _INST_B, "pi_name": "PI History", "member_name": "PI History",
     "member_role": "PI", "department_program": "History",
     "lab_research_summary": "Medieval European trade routes and economic history",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
    {"institution": _INST_B, "pi_name": "PI History", "member_name": "Grad Student B",
     "member_role": "PhD Student", "department_program": "History",
     "lab_research_summary": "Medieval European trade routes and economic history",
     "lab_homepage_url": "", "lab_members_url": "", "status": "active"},
]


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


# ---------------------------------------------------------------------------
# Deactivation scope tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def deactivation_tab(client):
    """
    Seed the Test_Deactivation tab with 3 members each for TestInst_A and
    TestInst_B (all active), then yield the tab name.  Cleaned up after all
    deactivation tests complete.
    """
    client.delete_tab(DEACTIVATION_TAB)
    client.create_tab(DEACTIVATION_TAB)
    client.append_rows(DEACTIVATION_TAB, _MEMBERS_A_FULL + _MEMBERS_B_FULL)
    yield DEACTIVATION_TAB
    client.delete_tab(DEACTIVATION_TAB)


def test_deactivation_scoped_to_inst_a(client, deactivation_tab):
    """
    Running with institutions_in_run=["TestInst_A"] and only 2 of 3 TestInst_A
    members should deactivate the missing A member but NOT touch TestInst_B rows.
    """
    config = load_config()
    init_run_cache()

    result = write_pipeline1_results(
        _MEMBERS_A_PARTIAL,
        config,
        institutions_in_run=[_INST_A],
        tab_name=deactivation_tab,
        deactivate_missing=True,
    )

    rows = client.read_all_rows(deactivation_tab)
    by_name = {r["member_name"]: r for r in rows}

    # Carol was in the original sheet but not in the partial batch
    assert by_name["Carol"]["status"] == "inactive", (
        "Carol (missing A member) should be deactivated"
    )

    # Alice and Bob remain active
    assert by_name["Alice"]["status"] == "active"
    assert by_name["Bob"]["status"] == "active"

    # All TestInst_B members are untouched
    for name in ("Dave", "Eve", "Frank"):
        assert by_name[name]["status"] == "active", (
            f"{name} (TestInst_B) should not have been touched"
        )

    # institution_last_scraped set for TestInst_A rows, not TestInst_B
    assert by_name["Alice"].get("institution_last_scraped"), (
        "Alice should have institution_last_scraped set"
    )
    assert by_name["Bob"].get("institution_last_scraped"), (
        "Bob should have institution_last_scraped set"
    )
    assert not by_name["Dave"].get("institution_last_scraped"), (
        "Dave (TestInst_B) should not have institution_last_scraped set"
    )

    deactivated = result["deactivated"]
    assert deactivated == 1, f"Expected 1 deactivated, got {deactivated}"

    print(f"\n{'='*60}")
    print(f"Deactivation test: {result}")
    print(f"{'='*60}")


def test_reactivation_when_member_returns(client, deactivation_tab):
    """
    When Carol (previously deactivated) reappears in a subsequent run,
    she should be reactivated to status='active'.
    """
    config = load_config()
    init_run_cache()

    # Now run with the full TestInst_A roster (Carol included)
    result = write_pipeline1_results(
        _MEMBERS_A_FULL,
        config,
        institutions_in_run=[_INST_A],
        tab_name=deactivation_tab,
        deactivate_missing=True,
    )

    rows = client.read_all_rows(deactivation_tab)
    by_name = {r["member_name"]: r for r in rows}

    assert by_name["Carol"]["status"] == "active", (
        "Carol should be reactivated when she reappears"
    )
    assert result["reactivated"] >= 1, (
        f"Expected at least 1 reactivated, got {result['reactivated']}"
    )

    print(f"\n{'='*60}")
    print(f"Reactivation test: {result}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Pipeline 2 full-scoring tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def p2_full_scoring_setup(client):
    """
    Seed the Test_P2_Scoring tab with members from two institutions:
    - TestInst_A: a clearly CRISPR-relevant PI
    - TestInst_B: a clearly irrelevant PI (medieval history)

    Then run Pipeline 2 (no scholar, no dry-run so results are written).
    After writing, read back all rows and yield them.
    Clean up the tab after all tests complete.
    """
    config = load_config()

    client.delete_tab(P2_SCORING_TAB)
    client.create_tab(P2_SCORING_TAB)
    client.append_rows(P2_SCORING_TAB, _P2_MEMBERS)

    # Run P2 (not dry-run so write_pipeline2_results executes)
    run_pipeline2(
        situation="CRISPR gene editing for cancer therapy",
        dry_run=False,
        skip_scholar=True,
        tab_name=P2_SCORING_TAB,
    )

    # Read back updated rows for assertions
    rows = client.read_all_rows(P2_SCORING_TAB)
    yield rows

    client.delete_tab(P2_SCORING_TAB)


def test_p2_all_active_rows_have_situation(p2_full_scoring_setup):
    """Every active row should have situation_of_interest set after P2 runs."""
    rows = p2_full_scoring_setup
    active_rows = [r for r in rows if r.get("status", "active") != "inactive"]
    for row in active_rows:
        assert row.get("situation_of_interest"), (
            f"Row {row.get('member_name')} missing situation_of_interest"
        )


def test_p2_all_active_rows_have_relevance_flag(p2_full_scoring_setup):
    """Every active row should have relevance_flag set (true or false)."""
    rows = p2_full_scoring_setup
    active_rows = [r for r in rows if r.get("status", "active") != "inactive"]
    for row in active_rows:
        flag = row.get("relevance_flag", "")
        assert flag != "", (
            f"Row {row.get('member_name')} missing relevance_flag"
        )


def test_p2_all_active_rows_have_relevance_reasoning(p2_full_scoring_setup):
    """Every active row should have relevance_reasoning set."""
    rows = p2_full_scoring_setup
    active_rows = [r for r in rows if r.get("status", "active") != "inactive"]
    for row in active_rows:
        assert row.get("relevance_reasoning"), (
            f"Row {row.get('member_name')} missing relevance_reasoning"
        )


def test_p2_non_passing_pi_gets_not_relevant_reasoning(p2_full_scoring_setup):
    """
    Members under PI History (medieval trade routes) should be marked
    not relevant with the standard explanation.
    """
    rows = p2_full_scoring_setup
    history_rows = [r for r in rows if r.get("pi_name") == "PI History"]
    assert history_rows, "Expected rows for PI History"
    for row in history_rows:
        assert row.get("relevance_reasoning") == "PI lab not relevant to current situation", (
            f"Expected standard not-relevant reasoning for {row.get('member_name')}, "
            f"got: {row.get('relevance_reasoning')!r}"
        )
        assert str(row.get("relevance_flag", "")).upper() in ("FALSE", "0", ""), (
            f"PI History member {row.get('member_name')} should not be flagged relevant"
        )


def test_p2_coarse_filter_covers_all_institutions(client):
    """
    coarse_filter_pis reads active PIs from all institutions, not just one.
    Seed a scratch tab with PIs from two institutions and verify both appear
    in the coarse filter's output (or at least are evaluated).
    """
    config = load_config()
    scratch_tab = "Test_CoarseFilter"

    client.delete_tab(scratch_tab)
    client.create_tab(scratch_tab)
    client.append_rows(scratch_tab, _P2_MEMBERS)

    try:
        # coarse_filter_pis returns keys for PIs that PASS; we just need to
        # confirm it evaluated PIs from BOTH institutions (no institution filter)
        passing = coarse_filter_pis(
            "CRISPR gene editing for cancer therapy",
            config,
            tab_name=scratch_tab,
        )
        # At minimum, PI CRISPR should pass (relevant lab)
        passing_set = set(passing)
        assert any("PI CRISPR" in p for p in passing_set), (
            "PI CRISPR should pass the coarse filter for CRISPR situation"
        )
        # PI History should NOT pass (clearly irrelevant)
        assert not any("PI History" in p for p in passing_set), (
            "PI History should not pass the coarse filter for CRISPR situation"
        )
    finally:
        client.delete_tab(scratch_tab)
