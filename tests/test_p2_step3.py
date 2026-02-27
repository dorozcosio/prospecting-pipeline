"""
Integration test for Pipeline 2, Step 3: fine-grained relevance scoring.

Inserts 5 synthetic member rows into a "Test_P3" tab (3 relevant to
"CRISPR gene editing for cancer therapy", 2 not), provides mock scholar
results, runs score_relevance, and verifies the classification.
"""
import pytest

from src.config import load_config
from src.pipeline2.step2_scholar_lookup import ScholarResult
from src.pipeline2.step3_fine_scoring import score_relevance
from src.sheets import SheetsClient

TAB_NAME = "Test_P3"
SITUATION = "CRISPR gene editing for cancer therapy"

# ---------------------------------------------------------------------------
# Synthetic sheet rows — written directly to the test tab
# ---------------------------------------------------------------------------
# All belong to the same fake PI so candidate_pis is simple.
CANDIDATE_PI = "Jane Smith|||MIT"

SHEET_ROWS = [
    # --- Relevant members ---
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 tools for oncogenic mutation correction "
            "and T-cell engineering."
        ),
        "member_name": "Alice Chen",
        "member_role": "PhD Student",
        "status": "active",
    },
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 tools for oncogenic mutation correction "
            "and T-cell engineering."
        ),
        "member_name": "Bob Rivera",
        "member_role": "Postdoc",
        "status": "active",
    },
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 tools for oncogenic mutation correction "
            "and T-cell engineering."
        ),
        "member_name": "Carol Kim",
        "member_role": "Research Scientist",
        "status": "active",
    },
    # --- Irrelevant members ---
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 tools for oncogenic mutation correction "
            "and T-cell engineering."
        ),
        "member_name": "Dave Nguyen",
        "member_role": "PhD Student",
        "status": "active",
    },
    {
        "institution": "MIT",
        "pi_name": "Jane Smith",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 tools for oncogenic mutation correction "
            "and T-cell engineering."
        ),
        "member_name": "Eva Schulz",
        "member_role": "Undergraduate",
        "status": "active",
    },
]

# ---------------------------------------------------------------------------
# Mock scholar results — papers steer the model toward correct classifications
# ---------------------------------------------------------------------------
MOCK_SCHOLAR: dict[tuple[str, str], ScholarResult] = {
    ("Jane Smith", "Alice Chen"): ScholarResult(
        status="found",
        papers=[
            "CRISPR-Cas9 base editing corrects oncogenic KRAS mutations in pancreatic organoids",
            "Homology-directed repair efficiency in primary T cells for adoptive cell therapy",
            "High-fidelity Cas9 variants reduce off-target editing in tumour suppressor loci",
        ],
    ),
    ("Jane Smith", "Bob Rivera"): ScholarResult(
        status="found",
        papers=[
            "AAV-delivered CRISPR disrupts PD-1 checkpoint in CAR-T cells",
            "In vivo somatic genome editing reverses oncogenic gain-of-function TP53 mutations",
            "Non-viral lipid nanoparticle delivery of Cas9 RNP for solid-tumour therapy",
        ],
    ),
    ("Jane Smith", "Carol Kim"): ScholarResult(
        status="found",
        papers=[
            "Enhancing HDR templating fidelity for therapeutic editing in AML",
            "CRISPR screens reveal synthetic lethality in BRCA-deficient cancers",
            "Prime editing corrects leukaemia-driving splice-site mutations ex vivo",
        ],
    ),
    ("Jane Smith", "Dave Nguyen"): ScholarResult(
        status="found",
        papers=[
            "Software-defined networking for low-latency data centre fabrics",
            "Adaptive congestion control in multi-tenant cloud environments",
            "RDMA over converged Ethernet: performance characterisation",
        ],
    ),
    ("Jane Smith", "Eva Schulz"): ScholarResult(
        status="found",
        papers=[
            "Aerosol-cloud interactions under high-SSP forcing scenarios",
            "Parameterisation of convective precipitation in global climate models",
            "Machine learning emulators for atmospheric radiative transfer",
        ],
    ),
}

EXPECTED_RELEVANT = {"Alice Chen", "Bob Rivera", "Carol Kim"}
EXPECTED_NOT_RELEVANT = {"Dave Nguyen", "Eva Schulz"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    return SheetsClient(load_config())


@pytest.fixture(autouse=True, scope="module")
def setup_and_teardown(client):
    """Create Test_P3 tab with test rows before module; delete after."""
    client.delete_tab(TAB_NAME)
    client.create_tab(TAB_NAME)
    client.append_rows(TAB_NAME, SHEET_ROWS)
    yield
    client.delete_tab(TAB_NAME)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_score_relevance():
    config = load_config()
    results = score_relevance(
        candidate_pis=[CANDIDATE_PI],
        scholar_results=MOCK_SCHOLAR,
        situation=SITUATION,
        config=config,
        tab_name=TAB_NAME,
    )

    assert results, "score_relevance returned an empty list"
    assert len(results) == len(SHEET_ROWS), (
        f"Expected {len(SHEET_ROWS)} results, got {len(results)}"
    )

    # Validate required keys
    required_keys = {"member_name", "pi_name", "institution", "relevance_flag", "relevance_reasoning"}
    for r in results:
        missing = required_keys - set(r.keys())
        assert not missing, f"Result missing keys {missing}: {r}"
        assert isinstance(r["relevance_flag"], bool), (
            f"relevance_flag must be bool, got {type(r['relevance_flag'])}: {r}"
        )

    by_name = {r["member_name"]: r for r in results}

    # Assert relevant researchers are flagged true
    for name in EXPECTED_RELEVANT:
        assert by_name[name]["relevance_flag"] is True, (
            f"Expected {name} to be relevant. "
            f"reasoning: {by_name[name]['relevance_reasoning']}"
        )

    # Assert irrelevant researchers are flagged false
    for name in EXPECTED_NOT_RELEVANT:
        assert by_name[name]["relevance_flag"] is False, (
            f"Expected {name} to be NOT relevant. "
            f"reasoning: {by_name[name]['relevance_reasoning']}"
        )

    # Print for manual inspection
    print(f"\n{'='*65}")
    print(f"Situation: {SITUATION}")
    print(f"{'='*65}")
    for r in results:
        flag = "YES" if r["relevance_flag"] else "NO "
        print(f"  [{flag}] {r['member_name']:<30} | {r['relevance_reasoning']}")
